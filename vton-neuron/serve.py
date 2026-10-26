"""
FastAPI inference server for Qwen-Image-Edit on Trainium2.

Models are loaded ONCE at startup and reused across all requests.

Usage:
    NEURON_RT_NUM_CORES=32 python serve.py \
        --compiled_models_dir /mnt/nvme/compiled_models_tp16 \
        --height 1024 --width 512 \
        --use_v3_cfg --patch_multiplier 3 \
        --port 8080

Request (multipart/form-data):
    POST /infer
      - image1: file           (required - garment image)
      - image2: file           (optional - person/model image)
      - prompt: str            (required)
      - negative_prompt: str   (optional)
      - num_inference_steps: int  (optional, default from CLI)
      - true_cfg_scale: float  (optional, default from CLI)
      - seed: int              (optional, default 42)

Response:
    image/png  (edited output image)
    Header: X-Inference-Time  (seconds)
"""

import os
import argparse

# ── Neuron env vars must be set before any torch/neuron imports ──────────────
WORLD_SIZE = int(os.environ.get("NEURON_RT_NUM_CORES", "8"))
os.environ["LOCAL_WORLD_SIZE"] = str(WORLD_SIZE)
os.environ["NEURON_RT_VIRTUAL_CORE_SIZE"] = "2"
os.environ["NEURON_LOGICAL_NC_CONFIG"] = "2"
os.environ["NEURON_FUSE_SOFTMAX"] = "1"
os.environ["NEURON_CUSTOM_SILU"] = "1"

# Parse server args early so they're available at module level
_parser = argparse.ArgumentParser(description="Qwen-Image-Edit inference server")
_parser.add_argument("--host", type=str, default="0.0.0.0")
_parser.add_argument("--port", type=int, default=8080)
_parser.add_argument("--compiled_models_dir", type=str,
                     default="/mnt/nvme/compiled_models_tp16")
_parser.add_argument("--s3_models_uri", type=str,
                     default=os.environ.get("VTON_MODEL_S3_URI", ""),
                     help="S3 URI of compiled Neuron artifacts to sync at startup "
                          "(or set env VTON_MODEL_S3_URI). Empty = use the models already "
                          "present in --compiled_models_dir. The deployer passes this explicitly.")
_parser.add_argument("--height", type=int, default=1024)
_parser.add_argument("--width", type=int, default=512)
_parser.add_argument("--patch_multiplier", type=int, default=3)
_parser.add_argument("--num_inference_steps", type=int, default=50)
_parser.add_argument("--true_cfg_scale", type=float, default=3.0)
_parser.add_argument("--use_v3_cfg", action="store_true", default=True)
SERVER_ARGS = _parser.parse_args()

import io
import random
import tempfile
import time
import types
import asyncio
import base64
import json
import uuid as _uuid
import urllib.request

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Depends, Request
from fastapi.responses import Response, HTMLResponse, JSONResponse
from PIL import Image

from diffusers import QwenImageEditPlusPipeline
from diffusers.utils import load_image
import run_qwen_image_edit as inference_module

app = FastAPI(title="Qwen-Image-Edit Inference Server")

PREFIX = os.environ.get("PATH_PREFIX", "/vton")

# Global: pipeline loaded once at startup
_pipe = None

# ── Optional Cognito JWT auth (env-gated) ────────────────────────────────────
# The real security boundary. OFF unless COGNITO_USER_POOL_ID is set (pure
# internal deploys stay auth-free). Stateless: verifies the Bearer token's
# signature against the pool's JWKS — no passwords/sessions/brute-force surface.
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_REGION = os.environ.get("COGNITO_REGION", os.environ.get("AWS_REGION", "us-east-2"))
COGNITO_APP_CLIENT_ID = os.environ.get("COGNITO_APP_CLIENT_ID", "")
COGNITO_HOSTED_UI = os.environ.get("COGNITO_HOSTED_UI", "")  # e.g. https://<domain>.auth.<region>.amazoncognito.com
AUTH_ENABLED = bool(COGNITO_USER_POOL_ID)
_JWKS_CACHE = None


def _get_jwks():
    global _JWKS_CACHE
    if _JWKS_CACHE is None:
        url = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}/.well-known/jwks.json"
        with urllib.request.urlopen(url, timeout=5) as r:
            _JWKS_CACHE = json.loads(r.read().decode())
    return _JWKS_CACHE


def _verify_cognito_jwt(token: str):
    """Verify a Cognito JWT (RS256) against the pool JWKS. Raises on failure."""
    import jwt  # PyJWT[crypto] — only required when AUTH_ENABLED
    from jwt import PyJWKClient
    unverified = jwt.get_unverified_header(token)
    jwks = _get_jwks()
    key = next((k for k in jwks["keys"] if k["kid"] == unverified["kid"]), None)
    if key is None:
        _JWKS_CACHE_reset()  # kid rotation — refetch once
        key = next((k for k in _get_jwks()["keys"] if k["kid"] == unverified["kid"]), None)
    if key is None:
        raise ValueError("signing key not found")
    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
    opts = {"verify_aud": bool(COGNITO_APP_CLIENT_ID)}
    return jwt.decode(
        token, public_key, algorithms=["RS256"],
        audience=COGNITO_APP_CLIENT_ID or None,
        issuer=f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}",
        options=opts,
    )


def _JWKS_CACHE_reset():
    global _JWKS_CACHE
    _JWKS_CACHE = None


def require_auth(request: Request):
    """FastAPI dependency: no-op when auth is disabled; else enforce a valid Cognito Bearer token."""
    if not AUTH_ENABLED:
        return None
    hdr = request.headers.get("authorization", "")
    token = hdr[7:].strip() if hdr.lower().startswith("bearer ") else hdr.strip()
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        return _verify_cognito_jwt(token)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=401, detail=f"invalid token: {e}")


def _base_args() -> types.SimpleNamespace:
    """Build a base args Namespace with server-level defaults."""
    return types.SimpleNamespace(
        images=[],
        prompt="",
        negative_prompt="",
        output=None,
        height=SERVER_ARGS.height,
        width=SERVER_ARGS.width,
        patch_multiplier=SERVER_ARGS.patch_multiplier,
        image_size=448,
        max_sequence_length=1024,
        vision_tp=False,
        cpu_language_model=True,
        neuron_language_model=False,
        use_v3_language_model=True,
        cpu_vision_encoder=False,
        neuron_vision_encoder=False,
        use_v3_vision_encoder=True,
        num_inference_steps=SERVER_ARGS.num_inference_steps,
        true_cfg_scale=SERVER_ARGS.true_cfg_scale,
        seed=42,
        compiled_models_dir=SERVER_ARGS.compiled_models_dir,
        vae_tile_size=512,
        use_v2=False,
        use_v1_flash=False,
        use_v2_flash=False,
        use_v3_cp=False,
        use_v3_cfg=SERVER_ARGS.use_v3_cfg,
        warmup=False,
        save_comparison=False,
        cpu_vae_decode=False,
        debug_text_encoder=False,
    )


@app.on_event("startup")
def load_pipeline():
    """Load the pipeline and all compiled Neuron models once at startup."""
    global _pipe

    args = _base_args()
    models_dir = args.compiled_models_dir

    # Download compiled models from S3 if not available locally
    if not os.path.isdir(models_dir) or not os.listdir(models_dir):
        s3_uri = SERVER_ARGS.s3_models_uri
        print(f"\nCompiled models not found at {models_dir}")
        print(f"Downloading from {s3_uri} ...")
        os.makedirs(models_dir, exist_ok=True)
        import subprocess
        result = subprocess.run(
            ["aws", "s3", "sync", s3_uri, models_dir, "--region", os.environ.get("AWS_REGION", "us-east-2")],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"S3 download failed: {result.stderr}")
        print(f"Download complete: {models_dir}")

    dtype = torch.bfloat16

    print("\nLoading base pipeline...")

    # Override VAE_IMAGE_SIZE to match compiled dimensions (same as run_inference does)
    import diffusers.pipelines.qwenimage.pipeline_qwenimage_edit_plus as qwen_pipeline_module
    compiled_vae_pixels = args.height * args.width
    qwen_pipeline_module.VAE_IMAGE_SIZE = compiled_vae_pixels

    pipe = QwenImageEditPlusPipeline.from_pretrained(
        inference_module.MODEL_ID,
        torch_dtype=dtype,
        cache_dir=os.environ.get("HUGGINGFACE_CACHE_DIR", inference_module.HUGGINGFACE_CACHE_DIR),
        local_files_only=os.path.isdir(os.environ.get("HUGGINGFACE_CACHE_DIR", inference_module.HUGGINGFACE_CACHE_DIR)),
    )

    # Fix processor pixel constraints to match compiled vision encoder
    target_pixels = args.image_size * args.image_size
    pipe.processor.image_processor.min_pixels = target_pixels
    pipe.processor.image_processor.max_pixels = target_pixels

    print("Loading compiled Neuron models (this takes a few minutes)...")
    pipe = inference_module.load_all_compiled_models(args.compiled_models_dir, pipe, args)

    _pipe = pipe
    print("\nPipeline ready. Server accepting requests.")


@app.get(f"{PREFIX}/health")
@app.get("/health")
def health():
    # 200 only when the model is loaded — used as the k8s readiness gate.
    # Returning 200 while loading lets k8s route /infer traffic to a pod that
    # is still loading compiled models (minutes), so every request 503s.
    if _pipe is None:
        return JSONResponse({"status": "loading", "world_size": WORLD_SIZE}, status_code=503)
    return {"status": "ok", "world_size": WORLD_SIZE}


def _run_inference(image1_bytes: bytes, image2_bytes, prompt: str,
                   negative_prompt: str, num_inference_steps: int,
                   true_cfg_scale: float, seed: int):
    """Core inference shared by the sync /infer path and the async worker.

    Returns (png_bytes, elapsed_seconds). Blocking — callers on the event loop
    should run it via asyncio.to_thread.
    """
    if _pipe is None:
        raise RuntimeError("Pipeline not ready")

    print(f"[VTON-PROMPT] prompt={prompt!r} | negative={negative_prompt!r} | steps={num_inference_steps} cfg={true_cfg_scale} seed={seed}", flush=True)
    start = time.time()
    tmp_files = []
    try:
        def save_and_resize(data: bytes) -> str:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            tw, th = SERVER_ARGS.width, SERVER_ARGS.height
            w, h = img.size
            # Match the MODEL's expected size (512x1024) by CROPPING, not squeezing.
            # Scale so the image fully covers the target (content proportions intact — no
            # distortion), then centre-crop to the exact model size, trimming equal amounts
            # from left/right and top/bottom (image centre as reference). The input's own
            # aspect ratio is intentionally NOT kept; the output always matches the model.
            scale = max(tw / w, th / h)
            nw, nh = max(tw, round(w * scale)), max(th, round(h * scale))
            img = img.resize((nw, nh), Image.LANCZOS)
            left = (nw - tw) // 2
            top = (nh - th) // 2
            img = img.crop((left, top, left + tw, top + th))
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                img.save(f, format="PNG")
                return f.name

        tmp_files.append(save_and_resize(image1_bytes))
        if image2_bytes is not None:
            tmp_files.append(save_and_resize(image2_bytes))

        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        generator = torch.Generator().manual_seed(seed)

        source_images = [load_image(p) for p in tmp_files]
        input_images = source_images[0] if len(source_images) == 1 else source_images

        output = _pipe(
            image=input_images,
            prompt=prompt,
            negative_prompt=negative_prompt,
            height=SERVER_ARGS.height,
            width=SERVER_ARGS.width,
            true_cfg_scale=true_cfg_scale,
            num_inference_steps=num_inference_steps,
            generator=generator,
        )

        buf = io.BytesIO()
        output.images[0].save(buf, format="PNG")
        return buf.getvalue(), (time.time() - start)
    finally:
        for p in tmp_files:
            try:
                os.unlink(p)
            except OSError:
                pass


def _cost_usd(elapsed: float) -> float:
    """Approximate Trn2 cost for one inference (rate x time, prorated by cores)."""
    return round(32 * elapsed * 35.76 / 3600 / 128, 6)


@app.post(f"{PREFIX}/infer")
@app.post("/infer")
async def infer(
    image1: UploadFile = File(..., description="First input image (e.g. garment)"),
    image2: UploadFile = File(None, description="Second input image (e.g. person)"),
    prompt: str = Form(...),
    negative_prompt: str = Form("blurry, low quality, deformed, distorted"),
    num_inference_steps: int = Form(SERVER_ARGS.num_inference_steps),
    true_cfg_scale: float = Form(SERVER_ARGS.true_cfg_scale),
    seed: int = Form(42),
    _auth=Depends(require_auth),
):
    """Synchronous inference (the API). Returns PNG bytes.

    Auth is env-gated: a no-op for internal deploys (COGNITO_USER_POOL_ID unset,
    so the orchestrator calls tokenless), enforced for public/standalone deploys.
    """
    if _pipe is None:
        raise HTTPException(status_code=503, detail="Pipeline not ready yet")

    img1 = await image1.read()
    img2 = await image2.read() if image2 is not None else None
    try:
        png, elapsed = await asyncio.to_thread(
            _run_inference, img1, img2, prompt, negative_prompt,
            num_inference_steps, true_cfg_scale, seed,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))

    print(f"Inference done in {elapsed:.1f}s | steps={num_inference_steps} cfg={true_cfg_scale} seed={seed}")
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "X-Inference-Time": f"{elapsed:.2f}s",
            "X-Cost-USD": f"{_cost_usd(elapsed)}",
            "X-Model": "qwen-image-edit",
        },
    )


# ── Model discovery ─────────────────────────────────────────────────────────
@app.get(f"{PREFIX}/v1/models")
@app.get("/v1/models")
async def list_models(_auth=Depends(require_auth)):
    return {"models": [{
        "id": "qwen-image-edit",
        "version": os.environ.get("MODEL_VERSION", "v3_cfg"),
        "capabilities": ["image-edit", "virtual-try-on"],
        "inputs": ["image1", "image2", "prompt", "num_inference_steps", "true_cfg_scale", "seed"],
        "api": {"infer": "POST /infer (multipart/form-data -> image/png; X-Inference-Time, X-Cost-USD, X-Model headers)"},
    }], "auth_required": AUTH_ENABLED}


# ── Self-served HTML tester (works standalone; browser-only) ─────────────────
# ── Self-served HTML tester (works standalone; browser-only) ─────────────────
_TESTER_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Image-Edit (Qwen VTON) Tester</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 24px; max-width: 1000px; margin-inline: auto; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #888; font-size: 13px; margin-bottom: 18px; }
  .row { display: flex; gap: 16px; flex-wrap: wrap; }
  .card { border: 1px solid #8884; border-radius: 10px; padding: 14px; }
  label { display: block; font-size: 13px; font-weight: 600; margin: 10px 0 4px; }
  input[type=text], textarea, input[type=number] { width: 100%; box-sizing: border-box; padding: 8px; border: 1px solid #8886; border-radius: 6px; font: inherit; background: transparent; color: inherit; }
  textarea { min-height: 60px; }
  .imgbox { width: 160px; }
  .imgbox img { width: 160px; height: 200px; object-fit: cover; border-radius: 8px; border: 1px solid #8884; background: #8881; }
  button { padding: 9px 16px; border: 0; border-radius: 8px; background: #2563eb; color: #fff; font: inherit; font-weight: 600; cursor: pointer; }
  button.secondary { background: #8883; color: inherit; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .status { margin: 12px 0; font-size: 14px; }
  .result img { max-width: 320px; border-radius: 10px; border: 1px solid #8884; }
  .meta { font-size: 12px; color: #888; margin-top: 6px; }
  .badge { display: inline-block; background: #2563eb22; color: #2563eb; border: 1px solid #2563eb55; border-radius: 999px; padding: 1px 9px; font-size: 11px; font-weight: 700; }
  .hist { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 10px; }
  .hist .item { width: 200px; border: 1px solid #8884; border-radius: 10px; padding: 8px; }
  .hist .item img { width: 100%; border-radius: 6px; }
  .topbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  #loginBtn { display: none; }
</style>
</head>
<body>
<div class="topbar">
  <div>
    <h1>Image-Edit · Qwen VTON <span class="badge" id="modelBadge">model</span></h1>
    <div class="sub">Upload a person photo + a garment, describe the edit, and submit. Async: submit &rarr; poll &rarr; result.</div>
  </div>
  <button id="loginBtn" class="secondary" onclick="login()">Sign in</button>
</div>

<div class="row">
  <div class="card">
    <div class="row">
      <div class="imgbox"><label>Person (image2)</label><img id="personPrev"/><input type="file" id="person" accept="image/*"/></div>
      <div class="imgbox"><label>Garment (image1)</label><img id="garmentPrev"/><input type="file" id="garment" accept="image/*"/></div>
    </div>
    <label>Prompt</label>
    <textarea id="prompt">The person in image 2 wearing the garment from image 1. Keep the face, hair, pose, and background exactly the same.</textarea>
    <div class="row">
      <div style="flex:1"><label>Steps</label><input type="number" id="steps" value="50" min="1" max="100"/></div>
      <div style="flex:1"><label>CFG scale</label><input type="number" id="cfg" value="3.0" step="0.5" min="1" max="10"/></div>
      <div style="flex:1"><label>Seed</label><input type="number" id="seed" value="42"/></div>
    </div>
    <div style="margin-top:14px"><button id="submitBtn" onclick="submitJob()">Generate</button></div>
    <div class="status" id="status"></div>
    <div class="result" id="result"></div>
  </div>
</div>

<div class="card" style="margin-top:18px">
  <div class="topbar"><strong>History <span class="sub" id="histNote">(this session only — cleared on refresh)</span></strong>
    <button class="secondary" onclick="clearHistory()">Clear history</button></div>
  <div class="hist" id="history"></div>
</div>

<script>window.__CFG__ = __CONFIG__;</script>
<script>
const CFG = window.__CFG__;
const BASE = location.pathname.endsWith('/') ? location.pathname : location.pathname + '/';
let HISTORY = [];  // in-memory: cleared on refresh (per requirement)

document.getElementById('modelBadge').textContent = CFG.model;

// ---- Cognito Hosted-UI login (implicit flow); token reused as Bearer ----
function login() {
  const redirect = location.origin + location.pathname;
  const url = CFG.hostedUi + '/login?client_id=' + encodeURIComponent(CFG.clientId) +
    '&response_type=token&scope=openid&redirect_uri=' + encodeURIComponent(redirect);
  location.href = url;
}
(function captureToken() {
  if (location.hash) {
    const h = new URLSearchParams(location.hash.slice(1));
    const t = h.get('id_token');
    if (t) { sessionStorage.setItem('idToken', t); history.replaceState(null, '', location.pathname); }
  }
  if (CFG.authEnabled && !sessionStorage.getItem('idToken')) {
    document.getElementById('loginBtn').style.display = 'inline-block';
    document.getElementById('submitBtn').disabled = true;
    document.getElementById('status').textContent = 'Please sign in to use the API.';
  }
})();
function authHeaders() {
  const t = sessionStorage.getItem('idToken');
  return t ? { 'Authorization': 'Bearer ' + t } : {};
}

// ---- image previews ----
function preview(inputId, imgId) {
  const el = document.getElementById(inputId);
  el.addEventListener('change', () => {
    if (el.files[0]) document.getElementById(imgId).src = URL.createObjectURL(el.files[0]);
  });
}
preview('person', 'personPrev');
preview('garment', 'garmentPrev');

// ---- submit (synchronous /infer) ----
async function submitJob() {
  const person = document.getElementById('person').files[0];
  const garment = document.getElementById('garment').files[0];
  if (!garment) { alert('Please choose a garment image (image1).'); return; }
  const btn = document.getElementById('submitBtn');
  btn.disabled = true;
  const t0 = performance.now();
  const statusEl = document.getElementById('status');
  const resultEl = document.getElementById('result');
  resultEl.innerHTML = '';
  statusEl.textContent = 'Generating… (~30–40s, please wait)';

  const fd = new FormData();
  fd.append('image1', garment);
  if (person) fd.append('image2', person);
  fd.append('prompt', document.getElementById('prompt').value);
  fd.append('num_inference_steps', document.getElementById('steps').value);
  fd.append('true_cfg_scale', document.getElementById('cfg').value);
  fd.append('seed', document.getElementById('seed').value);

  try {
    const res = await fetch(BASE + 'infer', { method: 'POST', headers: authHeaders(), body: fd });
    const ttfb = Math.round(performance.now() - t0);
    if (res.status === 401) {
      statusEl.textContent = 'Unauthorized — please sign in.';
      document.getElementById('loginBtn').style.display = 'inline-block';
      btn.disabled = false; return;
    }
    if (!res.ok) {
      let detail = ''; try { detail = (await res.json()).detail || ''; } catch {}
      statusEl.textContent = 'Failed: HTTP ' + res.status + (detail ? ' — ' + detail : '');
      btn.disabled = false; return;
    }
    const blob = await res.blob();
    const dataUrl = URL.createObjectURL(blob);
    const model = res.headers.get('X-Model') || CFG.model;
    const infTime = res.headers.get('X-Inference-Time') || '?';
    const cost = res.headers.get('X-Cost-USD') || '?';
    const total = ((performance.now() - t0) / 1000).toFixed(1);
    resultEl.innerHTML = '<img src="' + dataUrl + '"/>' +
      '<div class="meta"><span class="badge">' + esc(model) + '</span> ' +
      'inference ' + esc(infTime) + ' · total ' + total + 's · TTFB ' + ttfb + 'ms · cost $' + esc(cost) + '</div>';
    addHistory(dataUrl, document.getElementById('prompt').value, { model, inference: infTime, cost }, total, ttfb);
    statusEl.textContent = 'Done.';
  } catch (e) { statusEl.textContent = 'Error: ' + e; }
  btn.disabled = false;
}

// ---- history (in-memory, cleared on refresh) ----
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function addHistory(dataUrl, prompt, m, total, ttfb) {
  HISTORY.unshift({ dataUrl, prompt, model: m.model || CFG.model, inference: m.inference, cost: m.cost, total, ttfb, ts: new Date().toLocaleTimeString() });
  renderHistory();
}
function renderHistory() {
  const el = document.getElementById('history');
  el.innerHTML = HISTORY.map(h =>
    '<div class="item"><img src="' + h.dataUrl + '"/>' +
    '<div class="meta"><span class="badge">' + esc(h.model) + '</span> ' + esc(h.ts) + '<br/>' +
    'inf ' + esc(h.inference ?? '?') + ' · TTFB ' + h.ttfb + 'ms · $' + esc(h.cost ?? '?') + '<br/>' +
    esc((h.prompt || '').slice(0, 70)) + '</div></div>'
  ).join('');
}
function clearHistory() { HISTORY = []; renderHistory(); }
</script>
</body>
</html>"""


def _tester_config_json() -> str:
    return json.dumps({
        "authEnabled": AUTH_ENABLED,
        "hostedUi": COGNITO_HOSTED_UI,
        "clientId": COGNITO_APP_CLIENT_ID,
        "model": "qwen-image-edit",
        "prefix": PREFIX,
    })


@app.get("/", response_class=HTMLResponse)
@app.get(f"{PREFIX}/", response_class=HTMLResponse)
def tester_page():
    return HTMLResponse(_TESTER_HTML.replace("__CONFIG__", _tester_config_json()))


if __name__ == "__main__":
    uvicorn.run(app, host=SERVER_ARGS.host, port=SERVER_ARGS.port)
