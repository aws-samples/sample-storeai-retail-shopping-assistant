"""LiveAvatar relay server — HTTP + WebSocket on port 8502.

Follow-up to PR #1's avatar hardening. Builds on the validated token-on-/ws relay
and adds the pieces the live deploy test called for:

- Authenticated, `sub`-scoped relay (env-gated on COGNITO_USER_POOL_ID). Missing/
  invalid tokens are rejected with 401 before the socket is accepted.
- Async, cached JWKS fetch (does not block the aiohttp event loop) with a refetch
  cooldown so an unknown `kid` cannot trigger repeated network calls.
- Explicit `token_use == "id"` and required `exp`/`sub` claims.
- A short-lived, single-use pairing nonce (POST /pair, authenticated) so the
  token-less avatar page can join the caller's `sub` group WITHOUT ever seeing the
  Cognito id token. The frontend mints a nonce and hands only that to the avatar
  tab (via window.open + postMessage with an origin allowlist).
- With Cognito unset (local demo), auth is skipped and all sockets share one group.
"""
import asyncio, json, os, pathlib, time, secrets
import aiohttp
from aiohttp import web

LIVEAVATAR_API = "https://api.liveavatar.com/v1"
API_KEY = os.getenv("LIVEAVATAR_API_KEY", "")
PORT = int(os.getenv("LIVEAVATAR_PORT", "8502"))
HTML_PATH = pathlib.Path(__file__).parent / "liveavatar_avatar.html"

_COGNITO_POOL = os.environ.get("COGNITO_USER_POOL_ID", "")
_COGNITO_REGION = os.environ.get("COGNITO_REGION", os.environ.get("AWS_REGION", "us-east-2"))
_COGNITO_CLIENT = os.environ.get("COGNITO_APP_CLIENT_ID", "")
_AUTH_ENABLED = bool(_COGNITO_POOL)
if _AUTH_ENABLED and not _COGNITO_CLIENT:
    raise RuntimeError("COGNITO_APP_CLIENT_ID is required when Cognito is enabled")
_ISSUER = f"https://cognito-idp.{_COGNITO_REGION}.amazonaws.com/{_COGNITO_POOL}"

_JWKS_CACHE = None
_JWKS_FETCHED_AT = 0.0
_JWKS_MIN_REFRESH = 60.0          # do not refetch on unknown kid more often than this
_JWKS_LOCK = asyncio.Lock()

_DEMO_GROUP = "_demo"
_clients: dict[str, set[web.WebSocketResponse]] = {}

_PAIR_TTL = 45.0                  # seconds a pairing nonce is valid
_pairings: dict[str, tuple[str, float]] = {}   # nonce -> (sub, expiry_monotonic)


async def _fetch_jwks():
    url = f"{_ISSUER}/.well-known/jwks.json"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            r.raise_for_status()
            return await r.json()


async def _get_jwks(force: bool = False):
    """Cached JWKS. `force` refetches once, but no more often than _JWKS_MIN_REFRESH."""
    global _JWKS_CACHE, _JWKS_FETCHED_AT
    async with _JWKS_LOCK:
        now = time.monotonic()
        stale_enough = (now - _JWKS_FETCHED_AT) >= _JWKS_MIN_REFRESH
        if _JWKS_CACHE is None or (force and stale_enough):
            _JWKS_CACHE = await _fetch_jwks()
            _JWKS_FETCHED_AT = now
        return _JWKS_CACHE


async def _verify_cognito(token: str):
    import jwt  # PyJWT[crypto]
    hdr = jwt.get_unverified_header(token)
    jwks = await _get_jwks()
    key = next((k for k in jwks["keys"] if k["kid"] == hdr.get("kid")), None)
    if key is None:  # possible kid rotation — one cooldown-limited refetch
        jwks = await _get_jwks(force=True)
        key = next((k for k in jwks["keys"] if k["kid"] == hdr.get("kid")), None)
    if key is None:
        raise ValueError("signing key not found")
    pub = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
    claims = jwt.decode(
        token, pub, algorithms=["RS256"],
        audience=_COGNITO_CLIENT or None,
        issuer=_ISSUER,
        options={"verify_aud": bool(_COGNITO_CLIENT), "require": ["exp", "sub"]},
    )
    if claims.get("token_use") != "id":
        raise ValueError("token_use must be id")
    if not claims.get("sub"):
        raise ValueError("sub must not be empty")
    return claims


def _bearer(request) -> str:
    tok = request.query.get("token", "")
    if tok:
        return tok.strip()
    hdr = request.headers.get("authorization", "")
    return hdr[7:].strip() if hdr.lower().startswith("bearer ") else hdr.strip()


def _prune_pairings():
    now = time.monotonic()
    for n in [n for n, (_s, exp) in _pairings.items() if exp < now]:
        _pairings.pop(n, None)


async def index(request):
    html = HTML_PATH.read_text().replace("__AVATAR_AUTH_ENABLED__", json.dumps(_AUTH_ENABLED))
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


async def health(request):
    return web.json_response({"status": "ok"})


async def avatars(request):
    curated = [
        {"id": "f86e8b45-3389-424a-b3d7-7f6e8729e36d", "name": "Marianne in Black Suit"},
        {"id": "8532b602-89e8-44fa-a9e2-5a4259a058cc", "name": "Marianne in Red Suit"},
        {"id": "246e8d9d-5826-4f49-b8a0-07cb73ff7556", "name": "Thaddeus in Black Suit"},
        {"id": "b4fc2d60-3b82-4694-b243-93e9d2bb0242", "name": "Anastasia in Grey Shirt"},
        {"id": "ab0765ad-69de-41fb-9f8a-bd01c3c52d6f", "name": "Alessandra in Grey Sweater"},
        {"id": "7001c332-8101-4e5a-b695-eac2a72d9568", "name": "Pedro in Blue Shirt"},
        {"id": "7a517e8e-b41f-49e7-b6b3-2cdfb4bbff1e", "name": "Pedro Sitting"},
        {"id": "5dd4d830-957a-419f-9334-0dc4399ada5d", "name": "Rika Sitting"},
        {"id": "509609b9-cda3-4f74-b1b2-97b4d98834fd", "name": "Anthony in White Suit"},
        {"id": "bfed3e3e-7d44-4fdb-b2be-ce9a9fd0b9b5", "name": "Amina in Blue Suit"},
        {"id": "03f8332d-9046-42a1-bff3-3b2309f77b58", "name": "Graham in Black Suit"},
        {"id": "9650a758-1085-4d49-8bf3-f347565ec229", "name": "Silas HR"},
        {"id": "7b888024-f8c9-4205-95e1-78ce01497bda", "name": "Shawn Therapist"},
    ]
    return web.json_response(curated)


async def token(request):
    if _AUTH_ENABLED:
        credential = _bearer(request)
        if not credential:
            return web.json_response({"error": "missing token"}, status=401)
        try:
            await _verify_cognito(credential)
        except Exception:
            return web.json_response({"error": "invalid token"}, status=401)
    body = await request.json()
    avatar_id = body.get("avatar_id")
    voice_id = body.get("voice_id", "")
    if not avatar_id:
        return web.json_response({"error": "avatar_id required"}, status=400)
    if not API_KEY:
        return web.json_response({"error": "Avatar video service is not configured"}, status=503)
    headers = {"X-API-KEY": API_KEY, "accept": "application/json", "content-type": "application/json", "User-Agent": "StoreAI/2.0"}
    token_body = {"mode": "FULL", "avatar_id": avatar_id, "avatar_persona": {"language": "en"}, "max_session_length": 1800}
    if voice_id:
        token_body["avatar_persona"]["voice_id"] = voice_id
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{LIVEAVATAR_API}/sessions/token", headers=headers, json=token_body) as r:
            token_data = await r.json()
    if "data" not in token_data or not token_data.get("data"):
        return web.json_response({"error": "token creation failed"}, status=502)
    return web.json_response({"session_token": token_data["data"]["session_token"]},
                             headers={"Cache-Control": "no-store"})


async def pair(request):
    """Authenticated: mint a short-lived, single-use nonce bound to the caller's `sub`.
    The frontend calls this with its id token and hands only the nonce to the avatar tab."""
    if not _AUTH_ENABLED:
        return web.json_response({"error": "pairing not required without Cognito"}, status=400)
    token_str = _bearer(request)
    if not token_str:
        return web.json_response({"error": "missing token"}, status=401)
    try:
        claims = await _verify_cognito(token_str)
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "invalid token"}, status=401)
    _prune_pairings()
    nonce = secrets.token_urlsafe(32)
    _pairings[nonce] = (claims["sub"], time.monotonic() + _PAIR_TTL)
    return web.json_response({"nonce": nonce, "expires_in": int(_PAIR_TTL)},
                             headers={"Cache-Control": "no-store"})


async def ws_handler(request):
    """A socket only receives messages from sockets with the same `sub`. Auth is via
    a Cognito id token (?token= / Bearer) or a single-use pairing nonce (?pair=)."""
    group = _DEMO_GROUP
    if _AUTH_ENABLED:
        nonce = request.query.get("pair", "")
        if nonce:
            _prune_pairings()
            entry = _pairings.pop(nonce, None)  # single-use
            if not entry or entry[1] < time.monotonic():
                return web.Response(status=401, text="invalid or expired pairing")
            group = entry[0]
        else:
            token_str = _bearer(request)
            if not token_str:
                return web.Response(status=401, text="missing token")
            try:
                claims = await _verify_cognito(token_str)
            except Exception:  # noqa: BLE001
                return web.Response(status=401, text="invalid token")
            group = claims.get("sub") or ""
            if not group:
                return web.Response(status=401, text="token missing sub")

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _clients.setdefault(group, set()).add(ws)
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                for c in list(_clients.get(group, ())):
                    if c is not ws and not c.closed:
                        await c.send_str(msg.data)
    finally:
        peers = _clients.get(group)
        if peers is not None:
            peers.discard(ws)
            if not peers:
                _clients.pop(group, None)
    return ws


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/avatar", index)
    app.router.add_get("/avatars", avatars)
    app.router.add_post("/token", token)
    app.router.add_post("/pair", pair)
    app.router.add_get("/ws", ws_handler)
    return app


app = make_app()

if __name__ == "__main__":
    print(f"LiveAvatar relay server on http://localhost:{PORT}")
    # Browser WS URLs contain credentials; don't emit query strings into access logs.
    web.run_app(app, port=PORT, access_log=None)
