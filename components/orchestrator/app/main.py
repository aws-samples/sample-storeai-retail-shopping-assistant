"""
AgentCore Runtime entrypoint for the StoreAI orchestrator.

AgentCore Runtime expects a FastAPI (or compatible ASGI) app exposed at
`app` in the module specified by the Runtime configuration. Streaming is
via Server-Sent Events on POST /invoke.

Run locally:
    uv run uvicorn app.main:app --reload --port 8080
"""
from __future__ import annotations

import base64
import json
import os
import threading
import uuid
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.memory import MemorySaver

from .graph import build_graph
from .state import ConvState, Mode, Stage, TurnInput, initial_state

# In-memory checkpointer: conversation state is per-pod (chat history persists in
# DynamoDB separately). Swap for a durable DynamoDB checkpointer for multi-pod
# persistence — see docs/known-limitations.md.
checkpointer = MemorySaver()
graph = build_graph(checkpointer=checkpointer)

# Max characters accepted for a single chat turn (bounds request size).
MAX_CHAT_CHARS = int(os.environ.get("MAX_CHAT_CHARS", "8000"))

app = FastAPI(title="StoreAI Orchestrator", version="0.1.0")

# CORS — defaults to "*" for the demo (the frontend is same-origin via CloudFront).
# Set CORS_ORIGINS (comma-separated) to restrict origins in production.
_cors_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Optional Cognito JWT auth (env-gated; defense-in-depth, same token as the edge) ──
# Off unless COGNITO_USER_POOL_ID is set, so the current deployment is unaffected.
# When set, every request (except /health + CORS preflight) must carry a valid
# Cognito Bearer token — the same token the frontend already attaches.
_COGNITO_POOL = os.environ.get("COGNITO_USER_POOL_ID", "")
_COGNITO_REGION = os.environ.get("COGNITO_REGION", os.environ.get("AWS_REGION", "us-east-2"))
_COGNITO_CLIENT = os.environ.get("COGNITO_APP_CLIENT_ID", "")
_AUTH_ENABLED = bool(_COGNITO_POOL)
_JWKS_CACHE = None


def _cognito_jwks():
    global _JWKS_CACHE
    if _JWKS_CACHE is None:
        import urllib.request as _u
        url = f"https://cognito-idp.{_COGNITO_REGION}.amazonaws.com/{_COGNITO_POOL}/.well-known/jwks.json"
        with _u.urlopen(url, timeout=5) as r:
            _JWKS_CACHE = json.loads(r.read().decode())
    return _JWKS_CACHE


def _verify_cognito(token: str):
    import jwt  # PyJWT[crypto] — only imported when auth is enabled
    hdr = jwt.get_unverified_header(token)
    key = next((k for k in _cognito_jwks()["keys"] if k["kid"] == hdr["kid"]), None)
    if key is None:  # kid rotation — refetch once
        globals()["_JWKS_CACHE"] = None
        key = next((k for k in _cognito_jwks()["keys"] if k["kid"] == hdr["kid"]), None)
    if key is None:
        raise ValueError("signing key not found")
    pub = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
    return jwt.decode(
        token, pub, algorithms=["RS256"],
        audience=_COGNITO_CLIENT or None,
        issuer=f"https://cognito-idp.{_COGNITO_REGION}.amazonaws.com/{_COGNITO_POOL}",
        options={"verify_aud": bool(_COGNITO_CLIENT)},
    )


if _AUTH_ENABLED:
    from fastapi.responses import JSONResponse as _JSONResponse

    @app.middleware("http")
    async def _cognito_auth_mw(request: Request, call_next):
        path = request.url.path
        # Public paths: health + image proxies (served to <img> tags, which cannot
        # carry a bearer token). Everything else requires a valid Cognito token.
        if request.method == "OPTIONS" or path.startswith(("/health", "/images", "/tryon-images", "/tryon-results", "/tryon-share")):
            return await call_next(request)
        hdr = request.headers.get("authorization", "")
        token = hdr[7:].strip() if hdr.lower().startswith("bearer ") else hdr.strip()
        if not token:
            return _JSONResponse({"detail": "missing bearer token"}, status_code=401)
        try:
            _verify_cognito(token)
        except Exception as e:  # noqa: BLE001
            return _JSONResponse({"detail": f"invalid token: {e}"}, status_code=401)
        return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Long-term memory (session summary) ─────────────────────────────────────────

_SESSION_END_MARKERS = ("[CHECKOUT_COMPLETE]", "[LOGOUT]")


def _write_session_memory(customer_id: str, session_id: str, messages: list[dict]) -> None:
    """Best-effort: summarize the session into long-term memory (fire-and-forget)."""
    try:
        from .gateway_client import get_backend
        get_backend().call("summarize_session", {
            "customer_id": customer_id,
            "session_id": session_id,
            "messages": messages,
        })
    except Exception:  # noqa: BLE001 — memory persistence must never break a turn
        pass


def _maybe_persist_session(reply: str, customer_id: str | None, session_id: str,
                           history: list, user_text: str = "") -> None:
    """If the reply signals session end and the customer is signed in, persist a
    session summary to long-term memory in a background thread (non-blocking)."""
    if not customer_id or not reply:
        return
    if not any(marker in reply for marker in _SESSION_END_MARKERS):
        return
    messages = list(history or [])
    if user_text:
        messages.append({"role": "user", "content": user_text})
    messages.append({"role": "assistant", "content": reply})
    threading.Thread(
        target=_write_session_memory,
        args=(customer_id, session_id, messages),
        daemon=True,
    ).start()


@app.post("/end_session")
async def end_session(request: Request):
    """Explicit session-end hook — the frontend calls this on logout or after
    checkout to persist a long-term memory summary for signed-in customers.
    Also triggered automatically from /chat when a session-end marker appears."""
    body = await request.json()
    customer_id = body.get("customer_id")
    session_id = body.get("session_id") or str(uuid.uuid4())
    messages = body.get("messages") or body.get("history") or []
    if not customer_id:
        return {"stored": False, "reason": "no customer_id (guests have no long-term memory)"}
    if not messages:
        return {"stored": False, "reason": "no messages"}
    from .gateway_client import get_backend
    result = get_backend().call("summarize_session", {
        "customer_id": customer_id,
        "session_id": session_id,
        "messages": messages,
    })
    if result.error:
        return {"stored": False, "error": result.error}
    return {
        "stored": result.result.get("stored", False),
        "summary": result.result.get("summary", ""),
    }


# ── LiveAvatar proxy (ALB routes all traffic to orchestrator) ──────────────────

@app.get("/avatar")
async def avatar_page():
    """Proxy the LiveAvatar HTML page from the liveavatar service."""
    import urllib.request
    from fastapi.responses import HTMLResponse
    try:
        r = urllib.request.urlopen("http://liveavatar-svc:8502/avatar", timeout=5)
        return HTMLResponse(content=r.read().decode(), status_code=200)
    except Exception as e:
        return HTMLResponse(content=f"<h3>Avatar service unavailable: {e}</h3>", status_code=503)


@app.get("/avatars")
async def avatars_proxy():
    """Proxy avatar list from liveavatar service."""
    import urllib.request
    try:
        r = urllib.request.urlopen("http://liveavatar-svc:8502/avatars", timeout=5)
        return json.loads(r.read().decode())
    except Exception:
        return []


@app.post("/token")
async def token_proxy(request: Request):
    """Proxy HeyGen token creation to liveavatar service."""
    import urllib.request
    body = await request.body()
    try:
        req = urllib.request.Request("http://liveavatar-svc:8502/token", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        r = urllib.request.urlopen(req, timeout=15)
        return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}


@app.post("/invoke")
async def invoke(request: Request):
    """Non-streaming invoke — returns the full reply as JSON."""
    body = await request.json()
    session_id = body.get("session_id") or str(uuid.uuid4())
    mode: Mode = body.get("mode", "standard")
    text: str = (body.get("message", "") or "")[:MAX_CHAT_CHARS]
    image_ref: str | None = body.get("image_ref")
    customer_id: str | None = body.get("customer_id")
    customer_name: str | None = body.get("customer_name")

    config = {"configurable": {"thread_id": session_id}}

    # Seed initial state on first turn.
    existing = checkpointer.get(config)
    if existing is None:
        seed = initial_state(session_id, mode)
        if customer_id:
            seed = {**seed, "customer_id": customer_id, "customer_name": customer_name}
        await graph.ainvoke(seed, config)

    turn_input = TurnInput(text=text, image_ref=image_ref)
    result: ConvState = await graph.ainvoke(
        {"turn_input": turn_input},
        config,
    )

    msgs = result.get("messages", [])
    reply = msgs[-1].content if msgs else ""
    return {
        "reply": reply,
        "stage": result.get("stage", Stage.GREET).value,
        "session_id": session_id,
        "customer_id": result.get("customer_id"),
        "pending_tryons": [
            {"job_id": j.job_id, "product_id": j.product_id}
            for j in result.get("pending_tryons", [])
        ],
    }


@app.post("/invoke/stream")
async def invoke_stream(request: Request):
    """Streaming invoke — SSE token stream."""
    body = await request.json()
    session_id = body.get("session_id") or str(uuid.uuid4())
    mode: Mode = body.get("mode", "standard")
    text: str = (body.get("message", "") or "")[:MAX_CHAT_CHARS]
    image_ref: str | None = body.get("image_ref")

    config = {"configurable": {"thread_id": session_id}}

    existing = checkpointer.get(config)
    if existing is None:
        seed = initial_state(session_id, mode)
        await graph.ainvoke(seed, config)

    turn_input = TurnInput(text=text, image_ref=image_ref)

    async def event_stream() -> AsyncIterator[str]:
        async for event in graph.astream_events(
            {"turn_input": turn_input}, config, version="v2"
        ):
            kind = event.get("event")
            if kind == "on_chat_model_stream":
                chunk: AIMessageChunk = event["data"]["chunk"]
                if chunk.content:
                    yield f"data: {json.dumps({'token': chunk.content})}\n\n"
            elif kind == "on_chain_end" and event.get("name") == "guardrail_out":
                state: ConvState = event["data"].get("output", {})
                yield f"data: {json.dumps({'done': True, 'stage': state.get('stage', 'greet')})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/chat")
async def chat_compat(request: Request):
    """V1-compatible /chat endpoint.

    Accepts the same request body as the v1 retail-api Lambda /chat endpoint
    and translates it to the v2 orchestrator format. This lets the existing
    frontend work without changes.
    """
    body = await request.json()
    session_id = body.get("session_id") or str(uuid.uuid4())
    # Map frontend's demo_mode + station to our internal mode
    demo_mode = body.get("demo_mode", "standard")
    station = body.get("station", "shopping")
    if demo_mode == "booth":
        mode: Mode = f"booth_{station}" if station in ("shopping", "tryon") else "booth_shopping"
    else:
        mode: Mode = "standard"
    text: str = (body.get("message", "") or "")[:MAX_CHAT_CHARS]
    customer_id: str | None = body.get("customer_id")
    customer_name: str | None = body.get("customer_name")
    image_ref: str | None = body.get("image_ref")
    history: list = body.get("history", [])

    config = {"configurable": {"thread_id": session_id}}

    # Seed initial state on first turn.
    existing = checkpointer.get(config)
    if existing is None:
        seed = initial_state(session_id, mode)
        if customer_id:
            seed = {**seed, "customer_id": customer_id, "customer_name": customer_name}
        await graph.ainvoke(seed, config)

    turn_input = TurnInput(text=text, image_ref=image_ref)
    # Pass history and customer context into the state for this turn
    invoke_input = {"turn_input": turn_input, "history": history}
    if customer_id:
        invoke_input["customer_id"] = customer_id
        invoke_input["customer_name"] = customer_name
    # VTON engine selection from the frontend (per-turn)
    _ve = body.get("vton_engine") or body.get("vtonEngine")
    if _ve:
        invoke_input["vton_engine"] = _ve
    _vs = body.get("vton_steps") or body.get("vtonSteps")
    if _vs:
        invoke_input["vton_steps"] = int(_vs)
    _vc = body.get("vton_cfg") or body.get("vtonCfgScale")
    if _vc:
        invoke_input["vton_cfg"] = float(_vc)
    result: ConvState = await graph.ainvoke(invoke_input, config)

    msgs = result.get("messages", [])
    reply = msgs[-1].content if msgs else ""
    stage = result.get("stage", Stage.GREET)

    # Build v1-compatible response
    # Get tool_results from the LLM agent run (stored in state by the graph)
    tool_results = result.get("_tool_results", [])

    # Extract cost from the special _cost tool result
    cost = None
    filtered_tool_results = []
    for tr in tool_results:
        if tr.get("tool") == "_cost":
            cost_raw = tr.get("result", "")
            if isinstance(cost_raw, str):
                try:
                    cost = json.loads(cost_raw)
                except (json.JSONDecodeError, TypeError):
                    pass
            else:
                cost = cost_raw
        else:
            filtered_tool_results.append(tr)

    _maybe_persist_session(
        reply, result.get("customer_id") or customer_id, session_id, history, body.get("message", "")
    )

    return {
        "reply": reply,
        "model": body.get("model", "orchestrator-v2"),
        "tool_results": filtered_tool_results,
        "cost": cost,
        "stage": stage.value if hasattr(stage, "value") else str(stage),
        "session_id": session_id,
        "customer_id": result.get("customer_id") or customer_id,
        "pending_tryons": [
            {"job_id": j.job_id, "product_id": j.product_id}
            for j in result.get("pending_tryons", [])
        ],
    }


# ── Streaming chat endpoint ─────────────────────────────────────────────────

@app.post("/chat/stream")
async def chat_stream(request: Request):
    """Streaming /chat — SSE tokens for instant perceived response.

    Same request format as /chat, but returns Server-Sent Events.
    Events: {token: "..."} for each text chunk, {done: true, ...} at end.
    """
    import time
    from fastapi.responses import StreamingResponse as SR

    body = await request.json()
    session_id = body.get("session_id") or str(uuid.uuid4())
    demo_mode = body.get("demo_mode", "standard")
    station = body.get("station", "shopping")
    if demo_mode == "booth":
        mode: Mode = f"booth_{station}" if station in ("shopping", "tryon") else "booth_shopping"
    else:
        mode: Mode = "standard"
    text: str = (body.get("message", "") or "")[:MAX_CHAT_CHARS]
    customer_id: str | None = body.get("customer_id")
    customer_name: str | None = body.get("customer_name")
    history: list = body.get("history", [])

    # Build system prompt and messages (same as non-streaming)
    from .prompts import build_system_prompt
    from .llm_agent import TOOL_DEFS, _exec_tool, _calc_cost
    from .llm_client import client as _gateway_client, CHAT_MODEL, bedrock_tools_to_openai, resolve_model
    from .state import initial_state, TurnInput, Stage
    from .nodes.intent_router import classify

    state = initial_state(session_id, mode)
    if customer_id:
        state = {**state, "customer_id": customer_id, "customer_name": customer_name}
    state["turn_input"] = TurnInput(text=text)
    state["history"] = history

    # Classify intent
    intent = classify(text)
    state["intent"] = intent

    system = build_system_prompt(state)
    model_id = resolve_model(body.get("model"))
    if model_id is None:
        _sel = body.get("model")
        async def _model_unavailable():
            yield "data: " + json.dumps({"error": "model_unavailable", "message": f"Selected model '{_sel}' is not available. Please choose a different model."}) + "\n\n"
            yield "data: " + json.dumps({"done": True, "error": "model_unavailable"}) + "\n\n"
        return SR(_model_unavailable(), media_type="text/event-stream")

    messages = [{"role": "system", "content": system}]
    for h in history[-12:]:
        role = h.get("role", "user")
        content = (h.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": text.strip() or "hello"})

    active_defs = TOOL_DEFS
    if mode == "booth_shopping":
        blocked = {"virtual_tryon", "check_photo", "recommend_size"}
        active_defs = [t for t in TOOL_DEFS if t["toolSpec"]["name"] not in blocked]
    active_tools = bedrock_tools_to_openai(active_defs)

    def _exec_vton_direct(inp):
        """Execute in-cluster VTON via the shared _run_vton helper. Returns a JSON string."""
        eng = inp.get("engine") or "qwen_image_edit"
        if eng not in ("fashn_vton", "qwen_image_edit"):
            eng = "qwen_image_edit"
        return json.dumps(_run_vton(
            eng, inp.get("customer_id"), inp.get("product_id"),
            steps=inp.get("vton_steps", 50),
            cfg_scale=inp.get("vton_cfg_scale"),
            seed=inp.get("vton_seed", 42),
        ))

    async def stream_events():
        import asyncio
        import queue
        tool_results_collected = []
        total_input = 0
        total_output = 0
        full_reply = ""
        t0 = time.time()

        for _ in range(5):
            try:
                stream = await asyncio.to_thread(
                    lambda: _gateway_client().chat.completions.create(
                        model=model_id,
                        messages=messages,
                        tools=active_tools,
                        max_tokens=2048,
                        temperature=0.5,
                        stream=True,
                        stream_options={"include_usage": True},
                    )
                )
            except Exception as e:
                yield f"data: {json.dumps({'token': 'I am having trouble right now. Could you try again?'})}\n\n"
                yield f"data: {json.dumps({'done': True, 'error': str(e)})}\n\n"
                return

            # Drain the synchronous OpenAI stream iterator via a worker thread → queue,
            # so the asyncio event loop can flush SSE chunks as they arrive.
            event_q: queue.Queue = queue.Queue()
            _SENTINEL = object()

            def _drain_stream():
                try:
                    for ch in stream:
                        event_q.put(ch)
                except Exception as ex:
                    event_q.put(ex)
                finally:
                    event_q.put(_SENTINEL)

            drain_task = asyncio.get_event_loop().run_in_executor(None, _drain_stream)

            acc_text = ""
            tool_calls: dict[int, dict] = {}
            vton_progress_sent: set[int] = set()
            finish_reason = ""

            while True:
                chunk = await asyncio.to_thread(event_q.get)
                if chunk is _SENTINEL:
                    break
                if isinstance(chunk, Exception):
                    break

                # Usage arrives on the final chunk (stream_options.include_usage)
                if getattr(chunk, "usage", None):
                    total_input += chunk.usage.prompt_tokens or 0
                    total_output += chunk.usage.completion_tokens or 0

                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = choice.delta
                if delta is None:
                    continue

                if delta.content:
                    acc_text += delta.content
                    full_reply += delta.content
                    yield f"data: {json.dumps({'token': delta.content})}\n\n"

                if delta.tool_calls:
                    for tcd in delta.tool_calls:
                        idx = tcd.index
                        slot = tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if tcd.id:
                            slot["id"] = tcd.id
                        if tcd.function and tcd.function.name:
                            slot["name"] = tcd.function.name
                            if slot["name"] == "virtual_tryon" and idx not in vton_progress_sent:
                                vton_progress_sent.add(idx)
                                progress = "\n\n✨ Generating your virtual try-on — hang tight, this takes about 15 seconds..."
                                full_reply += progress
                                yield f"data: {json.dumps({'token': progress})}\n\n"
                        if tcd.function and tcd.function.arguments:
                            slot["args"] += tcd.function.arguments

            await drain_task

            # Record the assistant turn (text + any tool calls)
            assistant_msg: dict = {"role": "assistant", "content": acc_text or None}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {"id": s["id"], "type": "function",
                     "function": {"name": s["name"], "arguments": s["args"] or "{}"}}
                    for s in tool_calls.values()
                ]
            messages.append(assistant_msg)

            if finish_reason != "tool_calls" or not tool_calls:
                break

            # Execute tool calls (VTON dispatched in-cluster; rest via MCP Lambdas)
            for s in tool_calls.values():
                name = s["name"]
                if name == "search_products":
                    yield f"data: {json.dumps({'token': ''})}\n\n"
                try:
                    tool_input = json.loads(s["args"] or "{}")
                except json.JSONDecodeError:
                    tool_input = {}
                if name == "virtual_tryon":
                    tool_input["engine"] = body.get("engine", "qwen_image_edit")
                    # Carry the UI's per-request VTON params (steps/cfg/seed) from the
                    # chat body into the tool args — the LLM never sets these itself.
                    for _k in ("vton_steps", "vton_cfg_scale", "vton_seed"):
                        if body.get(_k) is not None:
                            tool_input[_k] = body[_k]

                def _run_tool(nm, inp):
                    if nm == "virtual_tryon" and inp.get("engine") in ("fashn_vton", "qwen_image_edit"):
                        return _exec_vton_direct(inp)
                    return _exec_tool(nm, inp)

                result_str = await asyncio.to_thread(_run_tool, name, tool_input)
                tool_results_collected.append({"tool": name, "input": tool_input, "result": result_str})
                messages.append({"role": "tool", "tool_call_id": s["id"], "content": result_str})

        # If virtual_tryon returned a result_url, inject a short relative image URL.
        for tr in tool_results_collected:
            if tr["tool"] == "virtual_tryon":
                tr_result = tr["result"]
                if isinstance(tr_result, str):
                    try:
                        tr_result = json.loads(tr_result)
                    except (json.JSONDecodeError, TypeError):
                        tr_result = {}
                if tr_result.get("error") or tr_result.get("deferred"):
                    continue
                result_url = tr_result.get("result_url", "")
                if result_url and "[TRYON_IMG:" not in full_reply and "[TRYONIMG:" not in full_reply:
                    import re as _re
                    key_match = _re.search(r'tryon-results/([^?]+)', result_url)
                    short_url = f"/tryon-images/{key_match.group(1)}" if key_match else result_url
                    tag = f"\n\n[TRYON_IMG:{short_url}]"
                    full_reply += tag
                    yield f"data: {json.dumps({'token': tag})}\n\n"

        # Send final metadata
        cost = _calc_cost(model_id, total_input, total_output, len(tool_results_collected))
        elapsed = round(time.time() - t0, 2)
        _maybe_persist_session(full_reply, customer_id, session_id, history, body.get("message", ""))
        yield f"data: {json.dumps({'done': True, 'cost': cost, 'tool_results': tool_results_collected, 'elapsed': elapsed, 'session_id': session_id})}\n\n"

    return SR(stream_events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


# ── V1-compatible data endpoints (proxy to MCP Lambdas) ──────────────────────

@app.get("/get_product_details")
async def get_product_details(product_id: str):
    """Proxy to catalog-mcp Lambda."""
    from decimal import Decimal
    from .gateway_client import get_backend
    backend = get_backend()
    result = backend.call("get_product_details", {"product_id": product_id})
    # Convert Decimal strings to numbers
    data = result.result
    for key in ("price", "rating", "reviews_count"):
        if key in data and isinstance(data[key], str):
            try:
                data[key] = float(data[key])
            except (ValueError, TypeError):
                pass
    return data


@app.get("/search_products")
async def search_products_endpoint(request: Request):
    """Proxy to catalog-mcp Lambda."""
    from .gateway_client import get_backend
    params = dict(request.query_params)
    backend = get_backend()
    result = backend.call("search_products", params)
    # Convert Decimal strings to numbers in product list
    data = result.result
    for p in data.get("products", []):
        for key in ("price", "rating", "reviews_count"):
            if key in p and isinstance(p[key], str):
                try:
                    p[key] = float(p[key])
                except (ValueError, TypeError):
                    pass
    return data


@app.get("/get_cart")
async def get_cart_endpoint(customer_id: str):
    from .gateway_client import get_backend
    backend = get_backend()
    result = backend.call("get_cart", {"customer_id": customer_id})
    return result.result


@app.get("/get_customer_profile")
async def get_customer_profile_endpoint(request: Request):
    from .gateway_client import get_backend
    params = dict(request.query_params)
    backend = get_backend()
    result = backend.call("get_customer_profile", params)
    return result.result


@app.get("/get_tryon_room")
async def get_tryon_room_endpoint(customer_id: str):
    from .gateway_client import get_backend
    backend = get_backend()
    result = backend.call("get_tryon_room", {"customer_id": customer_id})
    return result.result


# ── Additional V1-compatible endpoints (stubs for frontend compatibility) ────

@app.get("/load_chat_history")
async def load_chat_history(session_id: str, limit: int = 20, last_key: str | None = None):
    """Load chat history from DynamoDB."""
    import boto3
    from boto3.dynamodb.conditions import Key
    try:
        table = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-2")).Table(f"storeai-{os.environ.get('ENV', 'dev')}-checkpoints")
        resp = table.query(
            KeyConditionExpression=Key("thread_id").eq(f"chat_{session_id}"),
            ScanIndexForward=False,   # newest first, so Limit keeps the MOST RECENT messages
            Limit=limit,
        )
        items = list(reversed(resp.get("Items", [])))  # back to chronological order for display
        messages = [{"role": i.get("role", "user"), "content": i.get("content", "")} for i in items]
        return {"messages": messages, "items": messages}
    except Exception:
        return {"messages": [], "items": []}


@app.post("/save_chat_message")
async def save_chat_message(request: Request):
    """Save chat message to DynamoDB."""
    import boto3
    import time
    body = await request.json()
    try:
        table = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-2")).Table(f"storeai-{os.environ.get('ENV', 'dev')}-checkpoints")
        table.put_item(Item={
            "thread_id": f"chat_{body.get('session_id', 'unknown')}",
            "checkpoint_id": f"msg_{int(time.time() * 1000)}",
            "role": body.get("role", "user"),
            "content": body.get("content", ""),
            "customer_id": body.get("customer_id", ""),
        })
    except Exception:
        pass
    return {"ok": True}


@app.get("/get_session_cost")
async def get_session_cost(session_id: str):
    return {"llm": 0, "vton": 0, "stt": 0, "infra": 0, "total": 0}


@app.get("/get_booth_queue")
async def get_booth_queue():
    """Return customers with booth_status=ready_for_tryon, sorted by booth_ready_at."""
    import boto3
    from boto3.dynamodb.conditions import Attr, Key
    try:
        dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-2"))
        customer_table = dynamodb.Table(f"storeai-{os.environ.get('ENV', 'dev')}-customers")
        cart_table = dynamodb.Table(f"storeai-{os.environ.get('ENV', 'dev')}-carts")
        tryon_room_table = dynamodb.Table(f"storeai-{os.environ.get('ENV', 'dev')}-tryon-room")

        resp = customer_table.scan(
            FilterExpression=Attr("booth_status").eq("ready_for_tryon"),
            ProjectionExpression="customer_id, first_name, last_name, phone, booth_ready_at",
        )
        items = sorted(resp.get("Items", []), key=lambda x: x.get("booth_ready_at", ""))

        for item in items:
            cid = item["customer_id"]
            cart_count = cart_table.query(
                KeyConditionExpression=Key("customer_id").eq(cid), Select="COUNT"
            ).get("Count", 0)
            tryon_count = tryon_room_table.query(
                KeyConditionExpression=Key("customer_id").eq(cid), Select="COUNT"
            ).get("Count", 0)
            item["cart_count"] = cart_count
            item["tryon_count"] = tryon_count
            item["total_items"] = cart_count + tryon_count

        return {"queue": items}
    except Exception as e:
        return {"queue": [], "error": str(e)}


@app.post("/add_to_cart")
async def add_to_cart_endpoint(request: Request):
    from .gateway_client import get_backend
    body = await request.json()
    backend = get_backend()
    result = backend.call("add_to_cart", body)
    return result.result


@app.post("/remove_from_cart")
async def remove_from_cart_endpoint(request: Request):
    from .gateway_client import get_backend
    body = await request.json()
    backend = get_backend()
    result = backend.call("remove_from_cart", body)
    return result.result


@app.post("/checkout")
async def checkout_endpoint(request: Request):
    from .gateway_client import get_backend
    body = await request.json()
    backend = get_backend()
    result = backend.call("checkout", body)
    return result.result


@app.post("/add_to_tryon_room")
async def add_to_tryon_room_endpoint(request: Request):
    from .gateway_client import get_backend
    body = await request.json()
    backend = get_backend()
    result = backend.call("add_to_tryon_room", body)
    return result.result


@app.post("/remove_from_tryon_room")
async def remove_from_tryon_room_endpoint(request: Request):
    from .gateway_client import get_backend
    body = await request.json()
    backend = get_backend()
    result = backend.call("remove_from_tryon_room", body)
    return result.result


@app.get("/check_photo")
async def check_photo_endpoint(customer_id: str):
    from .gateway_client import get_backend
    backend = get_backend()
    result = backend.call("check_photo", {"customer_id": customer_id})
    return result.result


@app.post("/upload_tryon_photo")
async def upload_tryon_photo_endpoint(request: Request):
    from .gateway_client import get_backend
    body = await request.json()
    backend = get_backend()
    result = backend.call("upload_tryon_photo", body)
    return result.result




def _run_vton(engine, cid, pid, steps=50, cfg_scale=None, seed=42):
    """Shared in-cluster virtual try-on for both the chat path and /virtual_try_on.

    Handles FASHN (GPU) and Qwen Image-Edit (Neuron). Returns a dict with
    {result_url, customer_id, product_id, engine, inference_sec, cost} on success, or
    {error, message?} (including the content-review outcome).
    """
    import boto3 as _boto3
    import urllib.request as _ur
    import time as _time
    import uuid as _uuid

    if not cid or not pid:
        return {"error": "customer_id and product_id required"}

    _s3 = _boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-2"))
    env = os.environ.get("ENV", "v2")
    tryon_bucket = f"storeai-{env}-tryon-{os.environ.get('ACCOUNT_ID', '')}"
    product_bucket = os.environ.get("PRODUCT_IMAGES_BUCKET", f"storeai-{env}-product-images-{os.environ.get('ACCOUNT_ID', '')}")

    try:
        _s3.head_object(Bucket=tryon_bucket, Key=f"photos/{cid}/photo.png")
    except Exception:
        return {"error": "no_photo", "message": "No photo uploaded. Please take your photo first."}
    photo_bytes = _s3.get_object(Bucket=tryon_bucket, Key=f"photos/{cid}/photo.png")["Body"].read()

    dynamodb = _boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-2"))
    prod = dynamodb.Table(f"storeai-{env}-products").get_item(Key={"product_id": pid}).get("Item", {})
    img_file = prod.get("image_file", "")
    if img_file:
        garment_bytes = _s3.get_object(Bucket=product_bucket, Key=f"images/{img_file}")["Body"].read()
    elif prod.get("image_url"):
        garment_bytes = _ur.urlopen(prod["image_url"], timeout=15).read()
    else:
        return {"error": f"No image for {pid}"}
    category = prod.get("category", "mens_tshirt")

    from . import vton_engines
    _files = [("image1", "garment.png", garment_bytes), ("image2", "person.png", photo_bytes)]

    if engine == "fashn_vton":
        fashn_url = os.environ.get("FASHN_VTON_URL", "http://storeai-fashn-vton:8081")
        fashn_cat = vton_engines.build_request("fashn_vton", category)["category"]
        boundary = _uuid.uuid4().hex
        body_parts = []
        for name, fname, data in _files:
            body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{fname}\"\r\nContent-Type: image/png\r\n\r\n".encode() + data + b"\r\n")
        for name, val in [("category", fashn_cat), ("num_inference_steps", "30"), ("seed", "42")]:
            body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode())
        body_parts.append(f"--{boundary}--\r\n".encode())
        t0 = _time.time()
        req = _ur.Request(f"{fashn_url}/infer", data=b"".join(body_parts), method="POST",
                          headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        resp = _ur.urlopen(req, timeout=120)
        result_bytes = resp.read()
        inference_time = float(resp.headers.get("X-Inference-Time") or (_time.time() - t0))
        result_key = f"tryon-results/{cid}/{pid}_{int(_time.time())}.png"
        _s3.put_object(Bucket=tryon_bucket, Key=result_key, Body=result_bytes, ContentType="image/png")
        result_url = _s3.generate_presigned_url("get_object", Params={"Bucket": tryon_bucket, "Key": result_key}, ExpiresIn=3600)
        return {"result_url": result_url, "customer_id": cid, "product_id": pid, "engine": "fashn_vton", "inference_sec": round(inference_time, 2), "cost": {"usd": 0.0}}

    if engine == "qwen_image_edit":
        qwen_url = os.environ.get("QWEN_VTON_URL", "http://storeai-vton:8081")
        _req = vton_engines.build_request(
            "qwen_image_edit", category,
            steps=int(steps or 50),
            cfg_override=(float(cfg_scale) if cfg_scale else None),
            seed=int(seed or 42),
        )
        boundary = _uuid.uuid4().hex
        body_parts = []
        for name, fname, data in _files:
            body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{fname}\"\r\nContent-Type: image/png\r\n\r\n".encode() + data + b"\r\n")
        for name, val in [("prompt", _req["prompt"]), ("negative_prompt", _req["negative_prompt"]), ("true_cfg_scale", str(_req["true_cfg_scale"])), ("num_inference_steps", str(_req["num_inference_steps"])), ("seed", str(_req["seed"]))]:
            body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode())
        body_parts.append(f"--{boundary}--\r\n".encode())
        t0 = _time.time()
        req = _ur.Request(f"{qwen_url}/infer", data=b"".join(body_parts), method="POST",
                          headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        resp = _ur.urlopen(req, timeout=180)
        result_bytes = resp.read()
        inference_time = _time.time() - t0
        from . import vton_verify
        final_bytes, engine_used = vton_verify.verify_or_fallback(result_bytes, garment_bytes, photo_bytes, category, _req["body_region"])
        if final_bytes is None:
            return {"error": vton_verify.CONTENT_REVIEW_ERROR, "message": vton_verify.CONTENT_REVIEW_MESSAGE}
        result_key = f"tryon-results/{cid}/{pid}_{int(_time.time())}.png"
        _s3.put_object(Bucket=tryon_bucket, Key=result_key, Body=final_bytes, ContentType="image/png")
        result_url = _s3.generate_presigned_url("get_object", Params={"Bucket": tryon_bucket, "Key": result_key}, ExpiresIn=3600)
        return {"result_url": result_url, "customer_id": cid, "product_id": pid, "engine": engine_used, "inference_sec": round(inference_time, 2), "cost": {"usd": round(32 * inference_time * 35.76 / 3600 / 128, 6)}}

    return {"error": "unsupported_engine", "message": f"Engine '{engine}' is not available in-cluster."}


@app.post("/virtual_try_on")
async def virtual_try_on_endpoint(request: Request):
    import urllib.request
    body = await request.json()
    engine = body.get("engine", "qwen_image_edit")
    print(f"[VTON-REQ] engine={engine} vton_steps={body.get('vton_steps')!r} vton_cfg_scale={body.get('vton_cfg_scale')!r} vton_seed={body.get('vton_seed')!r} keys={list(body.keys())}", flush=True)
    cid = body.get("customer_id")
    pid = body.get("product_id")

    if not cid or not pid:
        return {"error": "customer_id and product_id required"}

    # In-cluster engines (FASHN GPU, Qwen Image-Edit Neuron) run through the shared
    # _run_vton helper; any other engine routes through the MCP try-on Lambda below.
    # _run_vton is synchronous and slow (S3 reads, a 120-180s inference call, image
    # verification), so it goes to a worker thread. Calling it inline would block the
    # event loop for the whole try-on, which stalls /health and every other request in
    # this process. The streaming chat path already offloads the same helper, via
    # asyncio.to_thread(_run_tool, ...) below.
    if engine in ("fashn_vton", "qwen_image_edit"):
        import asyncio
        return await asyncio.to_thread(
            _run_vton,
            engine, cid, pid,
            steps=body.get("vton_steps", 50),
            cfg_scale=body.get("vton_cfg_scale"),
            seed=body.get("vton_seed", 42),
        )

    from .gateway_client import get_backend
    backend = get_backend()
    result = backend.call("virtual_tryon", body)
    return result.result


@app.get("/get_tryon_results")
async def get_tryon_results_endpoint(customer_id: str = None):
    from .gateway_client import get_backend
    backend = get_backend()
    params = {"customer_id": customer_id} if customer_id else {}
    result = backend.call("get_tryon_results", params)
    data = result.result
    # Add customer_name for the share page
    if customer_id and "customer_name" not in data:
        try:
            import boto3
            dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-2"))
            env = os.environ.get("ENV", "v2")
            cust = dynamodb.Table(f"storeai-{env}-customers").get_item(Key={"customer_id": customer_id}).get("Item", {})
            data["customer_name"] = f"{cust.get('first_name', '')} {cust.get('last_name', '')}".strip() or customer_id
        except Exception:
            data["customer_name"] = customer_id
    return data


@app.post("/register_customer")
async def register_customer_endpoint(request: Request):
    from .gateway_client import get_backend
    body = await request.json()
    backend = get_backend()
    result = backend.call("register_customer", body)
    return result.result


@app.post("/update_customer_profile")
async def update_customer_profile_endpoint(request: Request):
    from .gateway_client import get_backend
    body = await request.json()
    backend = get_backend()
    result = backend.call("update_profile", body)
    return result.result


@app.get("/get_order_history")
async def get_order_history_endpoint(customer_id: str):
    from .gateway_client import get_backend
    backend = get_backend()
    result = backend.call("get_order_history", {"customer_id": customer_id})
    return result.result


@app.get("/recommend_size")
async def recommend_size_endpoint(customer_id: str, product_id: str):
    from .gateway_client import get_backend
    backend = get_backend()
    result = backend.call("recommend_size", {"customer_id": customer_id, "product_id": product_id})
    return result.result


@app.post("/update_booth_status")
async def update_booth_status(request: Request):
    """Update booth status on customer record for booth demo handoff."""
    import boto3
    from datetime import datetime
    body = await request.json()
    cid = body.get("customer_id")
    status = body.get("booth_status")
    if not cid or status not in ("ready_for_tryon", "trying_on", "completed", "cleared"):
        return {"error": "customer_id and valid booth_status required"}
    try:
        table = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-2")).Table(f"storeai-{os.environ.get('ENV', 'dev')}-customers")
        if status in ("completed", "cleared"):
            table.update_item(
                Key={"customer_id": cid},
                UpdateExpression="REMOVE booth_status, booth_ready_at",
            )
        else:
            table.update_item(
                Key={"customer_id": cid},
                UpdateExpression="SET booth_status = :s, booth_ready_at = :t",
                ExpressionAttributeValues={":s": status, ":t": datetime.utcnow().isoformat()},
            )
        return {"message": f"Booth status set to {status}", "customer_id": cid}
    except Exception as e:
        return {"error": str(e)}


@app.post("/generate_tryon_qr")
async def generate_tryon_qr(request: Request):
    """Generate QR code linking to try-on results for the customer."""
    body = await request.json()
    cid = body.get("customer_id")
    if not cid:
        return {"error": "customer_id required"}
    try:
        import boto3
        import qrcode
        from io import BytesIO
        from PIL import Image, ImageDraw, ImageFont

        env = os.environ.get("ENV", "dev")
        region = os.environ.get("AWS_REGION", "us-east-2")
        bucket = f"storeai-{env}-tryon-{os.environ.get('ACCOUNT_ID', '')}"

        s3_client = boto3.client("s3", region_name=region)
        objs = s3_client.list_objects_v2(Bucket=bucket, Prefix=f"tryon-results/{cid}/")
        keys = [o["Key"] for o in sorted(objs.get("Contents", []), key=lambda x: x["LastModified"], reverse=True) if o["Key"].endswith(".png")][:3]
        if not keys:
            return {"qr_code_base64": "", "share_url": "", "image_count": 0}

        # Single-use share link (V1 design): mint an opaque token in the share-tokens
        # table (24h TTL backstop) recording this customer's result keys. The QR points
        # at the public /api/tryon-share endpoint, which serves the images once via
        # short-lived presigned URLs and then marks the token used.
        base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        if not base:
            return {"qr_code_base64": "", "share_url": "", "image_count": 0}
        from datetime import datetime, timedelta, timezone
        import uuid as _uuid
        token = _uuid.uuid4().hex
        ttl = int((datetime.now(timezone.utc) + timedelta(hours=24)).timestamp())
        boto3.resource("dynamodb", region_name=region).Table(f"storeai-{env}-share-tokens").put_item(
            Item={"token": token, "customer_id": cid, "s3_keys": keys, "used": False, "ttl": ttl}
        )
        share_url = f"{base}/api/tryon-share?token={token}"

        # Generate QR code
        qr = qrcode.QRCode(box_size=8, border=2, error_correction=qrcode.constants.ERROR_CORRECT_H)
        qr.add_data(share_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="#232f3e", back_color="#ffffff").convert("RGB")

        # Add "StoreAI" text below
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        except Exception:
            font = ImageFont.load_default()
        text = "StoreAI"
        draw_tmp = ImageDraw.Draw(qr_img)
        bbox = draw_tmp.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        final = Image.new("RGB", (qr_img.width, qr_img.height + th + 16), "#ffffff")
        final.paste(qr_img, (0, 0))
        draw = ImageDraw.Draw(final)
        draw.text(((final.width - tw) // 2, qr_img.height + 6), text, fill="#ff9900", font=font)
        buf = BytesIO()
        final.save(buf, format="PNG")
        return {"qr_code_base64": base64.b64encode(buf.getvalue()).decode(), "share_url": share_url, "image_count": len(keys)}
    except ImportError:
        return {"error": "qrcode/pillow not installed", "qr_code_base64": "", "image_count": 0}
    except Exception as e:
        return {"error": str(e), "qr_code_base64": "", "image_count": 0}


@app.get("/tryon-share")
async def tryon_share(request: Request):
    """Public single-use try-on gallery for a shared QR (V1 design).

    The token (minted by /generate_tryon_qr) is marked used atomically on first
    access; later visits show an 'already used' page. Images are served via
    short-lived (15-min) presigned S3 URLs embedded in a self-contained HTML page.
    Auth-exempt (orchestrator middleware + CloudFront edge) so a scanned phone can
    open it with no token.
    """
    from fastapi.responses import HTMLResponse
    import boto3
    from boto3.dynamodb.conditions import Attr

    token = request.query_params.get("token", "")
    if not token:
        return HTMLResponse("<h2>Invalid link</h2>", status_code=400)

    env = os.environ.get("ENV", "dev")
    region = os.environ.get("AWS_REGION", "us-east-2")
    table = boto3.resource("dynamodb", region_name=region).Table(f"storeai-{env}-share-tokens")
    try:
        resp = table.update_item(
            Key={"token": token},
            UpdateExpression="SET used = :t",
            ConditionExpression=Attr("used").eq(False),
            ExpressionAttributeValues={":t": True},
            ReturnValues="ALL_NEW",
        )
        item = resp["Attributes"]
    except Exception:
        return HTMLResponse(
            "<h2>This link has expired or already been used.</h2>"
            "<p>Try-on image links are single-use for your privacy.</p>",
            status_code=410,
        )

    keys = item.get("s3_keys", [])
    bucket = f"storeai-{env}-tryon-{os.environ.get('ACCOUNT_ID', '')}"
    s3_client = boto3.client("s3", region_name=region)
    images_html = ""
    for key in keys:
        url = s3_client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=900
        )
        images_html += (
            '<div style="margin:12px 0">'
            f'<img src="{url}" style="max-width:100%;border-radius:8px;'
            'box-shadow:0 2px 8px rgba(0,0,0,.15)"></div>'
        )
    html = (
        '<!DOCTYPE html><html><head><meta name="viewport" '
        'content="width=device-width,initial-scale=1">'
        '<title>Your Try-On Photos \u2014 StoreAI</title>'
        '<style>body{font-family:system-ui;max-width:480px;margin:0 auto;padding:20px;'
        'background:#f8f9fa}h2{color:#232f3e}.tip{background:#fff3cd;border:1px solid #ffc107;'
        'border-radius:8px;padding:12px;font-size:13px;color:#664d03;margin-bottom:16px}</style>'
        f'</head><body><h2>Your Try-On Photos</h2><p>{len(keys)} image(s)</p>'
        '<div class="tip">To save: long-press an image and choose '
        '<b>Save Image</b> or <b>Download Image</b>.</div>'
        f'{images_html}'
        '<p style="color:#888;font-size:13px;margin-top:24px">This link is single-use and is '
        'now expired. Images were available for 15 minutes.</p></body></html>'
    )
    return HTMLResponse(html)


@app.post("/save_measurements")
async def save_measurements_endpoint(request: Request):
    from .gateway_client import get_backend
    body = await request.json()
    backend = get_backend()
    result = backend.call("save_measurements", body)
    return result.result


@app.post("/photo_size_recommendation")
async def photo_size_recommendation_endpoint(request: Request):
    from .gateway_client import get_backend
    body = await request.json()
    backend = get_backend()
    result = backend.call("photo_size_recommendation", body)
    return result.result


@app.get("/images/{filename:path}")
async def serve_product_image(filename: str):
    """Serve product images from S3 via presigned URL redirect."""
    import boto3
    from fastapi.responses import RedirectResponse
    bucket = os.environ.get("PRODUCT_IMAGES_BUCKET", f"storeai-{os.environ.get('ENV', 'dev')}-product-images-{os.environ.get('ACCOUNT_ID', '')}")
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-2"))
    url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": f"images/{filename}"}, ExpiresIn=3600)
    return RedirectResponse(url=url, status_code=302)


@app.get("/tryon-images/{filepath:path}")
@app.get("/tryon-results/{filepath:path}")
async def serve_tryon_image(filepath: str, request: Request):
    """Serve try-on result images from S3 — proxied to avoid cross-origin ORB blocking.

    These are customer try-on photos (personal), so when auth is enabled they require
    a valid Cognito token. <img> tags can't send an Authorization header, so the token
    is accepted via ?token= query param (falling back to a bearer header for API use).
    """
    import boto3
    from fastapi.responses import Response
    if _AUTH_ENABLED:
        token = request.query_params.get("token", "")
        if not token:
            h = request.headers.get("authorization", "")
            token = h[7:].strip() if h.lower().startswith("bearer ") else h.strip()
        try:
            if not token:
                raise ValueError("missing token")
            _verify_cognito(token)
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=401, detail="unauthorized")
    env = os.environ.get("ENV", "v2")
    bucket = f"storeai-{env}-tryon-{os.environ.get('ACCOUNT_ID', '')}"
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-2"))
    try:
        obj = s3.get_object(Bucket=bucket, Key=f"tryon-results/{filepath}")
        return Response(
            content=obj["Body"].read(),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except Exception:
        raise HTTPException(status_code=404, detail="Image not found")
