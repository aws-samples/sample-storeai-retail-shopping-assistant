"""Offline validation of the follow-up relay server (async JWKS + pairing nonce).
No real Cognito: a local RSA keypair + fake JWKS. Run: python3 test_ws_auth.py

Covers the four deploy-validated cases against the async validator, plus the new
pairing-nonce protocol: mint via /pair, deliver by ?pair=, single-use, expiry,
and /pair authentication (id vs access, missing).
"""
import asyncio, json, time, importlib
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from aiohttp import web, ClientSession, WSServerHandshakeError
from aiohttp.test_utils import TestServer

REGION, POOL, CLIENT, KID = "us-east-2", "us-east-2_TEST", "test-client-id", "testkid1"
ISS = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"


def _pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())


_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_priv_pem = _pem(_key)
_JWKS = {"keys": [json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_key.public_key()))]}
_JWKS["keys"][0]["kid"] = KID


def mint(sub, aud=CLIENT, iss=ISS, key_pem=_priv_pem, kid=KID, token_use="id", exp_delta=3600):
    now = int(time.time())
    claims = {"sub": sub, "iss": iss, "iat": now, "exp": now + exp_delta, "token_use": token_use}
    if token_use == "id":
        claims["aud"] = aud
    else:
        claims["client_id"] = aud
    return jwt.encode(claims, key_pem, algorithm="RS256", headers={"kid": kid})


def load_server(auth_enabled: bool):
    import liveavatar_server as srv
    importlib.reload(srv)
    srv._AUTH_ENABLED = auth_enabled
    srv._COGNITO_POOL, srv._COGNITO_REGION, srv._COGNITO_CLIENT = POOL, REGION, CLIENT
    srv._ISSUER = ISS
    async def _fake_jwks(force=False):
        return _JWKS
    srv._get_jwks = _fake_jwks
    srv._clients = {}
    srv._pairings = {}
    return srv


async def recv_one(ws, timeout=2.0):
    return (await asyncio.wait_for(ws.receive(), timeout=timeout)).data


async def case_valid_delivery():
    srv = load_server(True)
    async with TestServer(srv.make_app()) as sv:
        base = f"http://{sv.host}:{sv.port}"
        async with ClientSession() as s:
            recv = await s.ws_connect(f"{base}/ws?token={mint('u1')}")
            send = await s.ws_connect(f"{base}/ws?token={mint('u1')}")
            await send.send_str("hello")
            assert await recv_one(recv) == "hello"
            await send.close(); await recv.close()
    return "valid id token: delivered to same-sub peer"


async def case_reject():
    srv = load_server(True)
    other = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    async with TestServer(srv.make_app()) as sv:
        base = f"http://{sv.host}:{sv.port}"
        res = {}
        async with ClientSession() as s:
            async def expect401(url, label):
                try:
                    await s.ws_connect(url); res[label] = "ACCEPTED(bad)"
                except WSServerHandshakeError as e:
                    res[label] = e.status
            await expect401(f"{base}/ws", "missing")
            await expect401(f"{base}/ws?token=not.a.jwt", "garbage")
            await expect401(f"{base}/ws?token={mint('u1', key_pem=other)}", "badsig")
            await expect401(f"{base}/ws?token={mint('u1', iss='https://evil')}", "badiss")
            await expect401(f"{base}/ws?token={mint('u1', exp_delta=-60)}", "expired")
            await expect401(f"{base}/ws?token={mint('u1', token_use='access')}", "access_token")
    assert all(v == 401 for v in res.values()), res
    return f"all rejected 401: {res}"


async def case_isolation():
    srv = load_server(True)
    async with TestServer(srv.make_app()) as sv:
        base = f"http://{sv.host}:{sv.port}"
        async with ClientSession() as s:
            r1 = await s.ws_connect(f"{base}/ws?token={mint('u1')}")
            r2 = await s.ws_connect(f"{base}/ws?token={mint('u2')}")
            snd = await s.ws_connect(f"{base}/ws?token={mint('u1')}")
            await snd.send_str("for u1")
            assert await recv_one(r1) == "for u1"
            leaked = False
            try:
                m = await asyncio.wait_for(r2.receive(), timeout=1.0)
                leaked = (m.type == web.WSMsgType.TEXT)
            except asyncio.TimeoutError:
                leaked = False
            assert not leaked, "u2 got a cross-sub message"
            await snd.close(); await r1.close(); await r2.close()
    return "two users isolated by sub"


async def case_demo():
    srv = load_server(False)
    async with TestServer(srv.make_app()) as sv:
        base = f"http://{sv.host}:{sv.port}"
        async with ClientSession() as s:
            recv = await s.ws_connect(f"{base}/ws")
            send = await s.ws_connect(f"{base}/ws")
            await send.send_str("demo")
            assert await recv_one(recv) == "demo"
            await send.close(); await recv.close()
    return "no-Cognito demo relays without a token"


async def case_pairing_flow():
    srv = load_server(True)
    async with TestServer(srv.make_app()) as sv:
        base = f"http://{sv.host}:{sv.port}"
        async with ClientSession() as s:
            # frontend mints a nonce with its id token
            async with s.post(f"{base}/pair", headers={"Authorization": f"Bearer {mint('u1')}"}) as r:
                assert r.status == 200, r.status
                nonce = (await r.json())["nonce"]
            # avatar tab connects with ONLY the nonce (no Cognito token)
            recv = await s.ws_connect(f"{base}/ws?pair={nonce}")
            # sender (authenticated) sends; paired receiver in same sub gets it
            send = await s.ws_connect(f"{base}/ws?token={mint('u1')}")
            await send.send_str("via nonce")
            assert await recv_one(recv) == "via nonce"
            await send.close(); await recv.close()
    return "pairing nonce joins caller's sub group without exposing the token"


async def case_pairing_single_use():
    srv = load_server(True)
    async with TestServer(srv.make_app()) as sv:
        base = f"http://{sv.host}:{sv.port}"
        async with ClientSession() as s:
            async with s.post(f"{base}/pair", headers={"Authorization": f"Bearer {mint('u1')}"}) as r:
                nonce = (await r.json())["nonce"]
            first = await s.ws_connect(f"{base}/ws?pair={nonce}")   # consumes it
            reused = "ACCEPTED(bad)"
            try:
                await s.ws_connect(f"{base}/ws?pair={nonce}")
            except WSServerHandshakeError as e:
                reused = e.status
            await first.close()
    assert reused == 401, reused
    return "pairing nonce is single-use (reuse -> 401)"


async def case_pairing_expiry_and_auth():
    srv = load_server(True)
    srv._PAIR_TTL = 0.3  # expire fast for the test
    async with TestServer(srv.make_app()) as sv:
        base = f"http://{sv.host}:{sv.port}"
        async with ClientSession() as s:
            async with s.post(f"{base}/pair", headers={"Authorization": f"Bearer {mint('u1')}"}) as r:
                nonce = (await r.json())["nonce"]
            await asyncio.sleep(0.5)
            expired = "ACCEPTED(bad)"
            try:
                await s.ws_connect(f"{base}/ws?pair={nonce}")
            except WSServerHandshakeError as e:
                expired = e.status
            # /pair rejects missing and access tokens
            async with s.post(f"{base}/pair") as r:
                miss = r.status
            async with s.post(f"{base}/pair", headers={"Authorization": f"Bearer {mint('u1', token_use='access')}"}) as r:
                acc = r.status
    assert expired == 401 and miss == 401 and acc == 401, (expired, miss, acc)
    return f"expired nonce->401; /pair missing->401; /pair access-token->401"


async def case_token_endpoint_auth():
    srv = load_server(True)
    async with TestServer(srv.make_app()) as sv:
        base = f"http://{sv.host}:{sv.port}"
        async with ClientSession() as s:
            for credential in [None, mint("u1", token_use="access")]:
                headers = {"Authorization": f"Bearer {credential}"} if credential else {}
                # Invalid JSON proves that authentication happens before body parsing.
                async with s.post(f"{base}/token", headers=headers, data="not JSON") as r:
                    assert r.status == 401, r.status
            async with s.post(f"{base}/token", headers={"Authorization": f"Bearer {mint('u1')}"},
                              json={}) as r:
                assert r.status == 400, r.status
    return "/token missing/access ->401 before parsing; valid id ->400 for empty payload"


async def case_claim_requirements():
    srv = load_server(True)
    base_claims = jwt.decode(mint("u1"), options={"verify_signature": False})
    variants = [
        {**base_claims, "sub": ""},
        {**base_claims, "aud": "another-client"},
        {**base_claims, "token_use": "access"},  # Keep valid aud to isolate token_use.
        {k: v for k, v in base_claims.items() if k != "exp"},
        {k: v for k, v in base_claims.items() if k != "sub"},
    ]
    async with TestServer(srv.make_app()) as sv:
        async with ClientSession() as s:
            for claims in variants:
                credential = jwt.encode(claims, _priv_pem, algorithm="RS256", headers={"kid": KID})
                async with s.post(f"http://{sv.host}:{sv.port}/pair",
                                  headers={"Authorization": f"Bearer {credential}"}) as r:
                    assert r.status == 401, r.status
    return "empty/missing sub, missing exp, wrong aud, access with valid aud ->401"


async def case_jwks_cooldown():
    import liveavatar_server as srv
    importlib.reload(srv)
    srv._COGNITO_CLIENT, srv._ISSUER = CLIENT, ISS
    calls = 0
    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return _JWKS
    srv._fetch_jwks = fetch
    await srv._verify_cognito(mint("u1"))
    async def unknown():
        try:
            await srv._verify_cognito(mint("u1", kid="unknown"))
        except ValueError:
            return
        raise AssertionError("Unknown key accepted")
    await asyncio.gather(*(unknown() for _ in range(20)))
    assert calls == 1, calls
    srv._JWKS_FETCHED_AT -= srv._JWKS_MIN_REFRESH + 1
    await asyncio.gather(*(unknown() for _ in range(20)))
    assert calls == 2, calls
    return "concurrent unknown kids: no extra fetch within cooldown, one fetch after cooldown"


async def main():
    cases = [
        ("1 valid-token delivery", case_valid_delivery),
        ("2 invalid/missing rejection (incl access token)", case_reject),
        ("3 two-user sub isolation", case_isolation),
        ("4 no-Cognito demo mode", case_demo),
        ("5 pairing-nonce delivery", case_pairing_flow),
        ("6 pairing-nonce single-use", case_pairing_single_use),
        ("7 pairing-nonce expiry + /pair auth", case_pairing_expiry_and_auth),
        ("8 /token authentication", case_token_endpoint_auth),
        ("9 required claims and explicit token_use", case_claim_requirements),
        ("10 async JWKS refresh cooldown", case_jwks_cooldown),
    ]
    ok = True
    for name, fn in cases:
        try:
            print(f"PASS  {name} — {await fn()}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"FAIL  {name} — {type(e).__name__}: {e}")
    print("\nALL PASS" if ok else "\nSOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
