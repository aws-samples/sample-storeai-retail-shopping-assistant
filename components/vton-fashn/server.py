"""FASHN VTON v1.5 inference server (fashn-ai/fashn-vton-1.5, Apache-2.0).

Wraps the open FASHN `TryOnPipeline` behind the /infer contract the StoreAI
orchestrator + tryon-mcp already call (multipart: image1=garment, image2=person,
category ∈ {tops,bottoms,one-pieces}, num_inference_steps, seed) → returns PNG
bytes + an X-Inference-Time header. Serves on :8081 as service storeai-fashn-vton.
"""
from __future__ import annotations

import io
import logging
import os
import time

import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response
from PIL import Image

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fashn-vton")

WEIGHTS_DIR = os.environ.get("WEIGHTS_DIR", "/weights")
VALID_CATEGORIES = {"tops", "bottoms", "one-pieces"}

# The internal ALB forwards the original request path unchanged, so a call routed by
# the `/vton-fashn` Ingress rule arrives here as `/vton-fashn/infer`, not `/infer`.
# Serve both forms: in-cluster callers (the orchestrator's `_run_vton` and the
# `vton_verify` FASHN fallback) reach this Service directly on the bare paths, while
# the tryon-mcp Lambda reaches it through the ALB. Mirrors vton-neuron/serve.py.
PREFIX = os.environ.get("PATH_PREFIX", "/vton-fashn").rstrip("/")

app = FastAPI(title="FASHN VTON v1.5")
_pipeline = None


def _load_pipeline():
    global _pipeline
    if _pipeline is None:
        from fashn_vton import TryOnPipeline  # imported lazily (heavy)
        log.info("Loading FASHN TryOnPipeline from %s ...", WEIGHTS_DIR)
        t0 = time.time()
        _pipeline = TryOnPipeline(weights_dir=WEIGHTS_DIR)
        log.info("Pipeline loaded in %.1fs", time.time() - t0)
    return _pipeline


@app.on_event("startup")
def _warm():
    # Warm the model at startup so the first real /infer is fast and readiness
    # (via /health) only passes once the model is actually loaded.
    try:
        _load_pipeline()
    except Exception:
        log.exception("Startup model load failed")


@app.get(f"{PREFIX}/health")
@app.get("/health")
def health():
    # 200 only when the model is loaded — used as the k8s readiness gate.
    if _pipeline is None:
        return JSONResponse({"status": "loading"}, status_code=503)
    return {"status": "ok", "model": "fashn-vton-1.5"}


@app.post(f"{PREFIX}/infer")
@app.post("/infer")
async def infer(
    image1: UploadFile = File(...),          # garment
    image2: UploadFile = File(...),          # person
    category: str = Form("tops"),            # tops | bottoms | one-pieces (already mapped by caller)
    num_inference_steps: str = Form("30"),
    seed: str = Form("42"),
    guidance_scale: str = Form("1.5"),
    garment_photo_type: str = Form("flat-lay"),  # store product shots are flat-lay
):
    garment = Image.open(io.BytesIO(await image1.read())).convert("RGB")
    person = Image.open(io.BytesIO(await image2.read())).convert("RGB")
    cat = category if category in VALID_CATEGORIES else "tops"

    t0 = time.time()
    result = _load_pipeline()(
        person_image=person,
        garment_image=garment,
        category=cat,
        num_timesteps=int(float(num_inference_steps)),
        guidance_scale=float(guidance_scale),
        seed=int(float(seed)),
        garment_photo_type=garment_photo_type,
    )
    elapsed = time.time() - t0
    buf = io.BytesIO()
    result.images[0].save(buf, format="PNG")
    log.info("try-on category=%s steps=%s done in %.2fs", cat, num_inference_steps, elapsed)
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"X-Inference-Time": f"{elapsed:.2f}"},
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8081, workers=1)
