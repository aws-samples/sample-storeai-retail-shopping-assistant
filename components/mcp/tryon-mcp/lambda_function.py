"""Try-On MCP — virtual_tryon, check_photo, upload_tryon_photo,
recommend_size, get_tryon_results, tryon_job_status.

Production-ready. VTON dispatches to SQS for async processing.
Job status reads from DynamoDB. Full VTON logic ported from v1.
"""
import json
import os
import uuid
import base64
import time

import boto3
from botocore.config import Config as _BotoConfig

_region = os.environ.get("AWS_REGION", "us-east-1")
s3 = boto3.client(
    "s3",
    region_name=_region,
    endpoint_url=f"https://s3.{_region}.amazonaws.com",
    config=_BotoConfig(signature_version="s3v4"),
)
sqs = boto3.client("sqs", region_name=_region)
dynamodb = boto3.resource("dynamodb", region_name=_region)
product_table = dynamodb.Table(os.environ.get("PRODUCT_TABLE", "Products"))
job_table = dynamodb.Table(os.environ.get("TRYON_JOB_TABLE", "TryOnJobs"))

TRYON_BUCKET = os.environ.get("TRYON_BUCKET", "")
PRODUCT_IMAGES_BUCKET = os.environ.get("PRODUCT_IMAGES_BUCKET", "")
VTON_QUEUE_URL = os.environ.get("VTON_QUEUE_URL", "")

# Ceiling on a decoded customer photo (see upload_tryon_photo).
MAX_PHOTO_BYTES = 6 * 1024 * 1024

# Leading bytes -> ContentType, for the formats a browser `accept="image/*"` picker
# realistically produces and Pillow can decode downstream. HEIC and friends are
# rejected here with a clear message rather than failing inside the try-on engine.
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def _sniff_image_type(data):
    """Return the ContentType implied by the leading bytes, or None if unrecognised."""
    for magic, content_type in _IMAGE_MAGIC:
        if data.startswith(magic):
            return content_type
    # WebP is RIFF-framed: "RIFF" <4-byte size> "WEBP".
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


CATEGORY_GARMENT_CLASS = {
    "mens_tshirt": "UPPER_BODY", "mens_polo": "UPPER_BODY", "mens_shirt": "UPPER_BODY",
    "mens_hoodie": "UPPER_BODY", "mens_jacket": "UPPER_BODY", "mens_sweater": "UPPER_BODY",
    "womens_tshirt": "UPPER_BODY", "womens_blouse": "UPPER_BODY", "womens_croptop": "UPPER_BODY",
    "womens_hoodie": "UPPER_BODY", "womens_cardigan": "UPPER_BODY",
    "mens_jeans": "LOWER_BODY", "mens_chinos": "LOWER_BODY", "mens_shorts": "LOWER_BODY",
    "mens_joggers": "LOWER_BODY", "womens_jeans": "LOWER_BODY", "womens_skirt": "LOWER_BODY",
    "womens_shorts": "LOWER_BODY", "womens_leggings": "LOWER_BODY",
    "womens_dress": "FULL_BODY",
    "sneakers": "FOOTWEAR", "running_shoes": "FOOTWEAR", "loafers": "FOOTWEAR",
    "boots": "FOOTWEAR", "sandals": "FOOTWEAR", "slippers": "FOOTWEAR",
}


def respond(code, body):
    return {"statusCode": code, "body": json.dumps(body, default=str)}


def _check_image_safety(image_bytes, garment_class, bedrock_client):
    """Check generated try-on image for inappropriate content using Nova Lite vision.

    Rules:
    - Torso must be properly covered by the garment (no exposed chest/midriff unless crop top)
    - No underwear or private areas visible
    - No wardrobe malfunction (garment improperly placed, body showing through)
    - Arms, lower legs (below knee), collarbone/neck area are ACCEPTABLE
    - For shorts/skirts: thighs above mid-thigh should not be excessively exposed
    """
    try:
        # Context-aware prompt based on garment type
        garment_context = {
            "UPPER_BODY": "The person should be wearing a top/shirt/jacket that covers the torso. Arms may be bare (t-shirt/tank). The chest should be covered with no cleavage or exposed stomach.",
            "LOWER_BODY": "The person should be wearing pants/shorts/skirt. For shorts: thighs may be partially visible but no underwear or excessive exposure. For skirts: knee-length or longer is expected.",
            "FULL_BODY": "The person should be wearing a dress or full outfit. The dress should properly cover the body — no exposed chest, stomach, or private areas.",
            "FOOTWEAR": "Focus is on shoes. The rest of the body should remain properly clothed as in the original photo.",
        }.get(garment_class, "The person should be appropriately dressed in clothing.")

        prompt = f"""Analyze this virtual try-on image for a retail clothing store demo.

Context: {garment_context}

Check for these DISQUALIFYING issues (answer YES if ANY are present):
1. Exposed chest, breasts, or significant cleavage beyond a normal neckline
2. Exposed stomach/midriff (unless the garment being tried is clearly a crop top)
3. Underwear, bra straps prominently visible, or private areas exposed
4. The garment is see-through revealing skin underneath
5. Wardrobe malfunction — garment not properly covering the body, slipping off

These are ACCEPTABLE and should NOT be flagged:
- Bare arms and forearms (normal for t-shirts, sleeveless tops)
- Lower legs below the knee (normal for shorts, skirts, dresses)
- Neck, collarbone, and upper chest area (normal neckline)
- Ankles and feet (normal for any outfit)

Respond with ONLY a JSON object:
{{"safe": true}} if the image is appropriate for a public retail demo
{{"safe": false, "reason": "brief description of issue", "suggestion": "how to fix"}} if inappropriate"""

        resp = bedrock_client.converse(
            modelId="us.amazon.nova-lite-v1:0",
            messages=[{
                "role": "user",
                "content": [
                    {"image": {"format": "png", "source": {"bytes": image_bytes}}},
                    {"text": prompt},
                ]
            }],
            inferenceConfig={"maxTokens": 150, "temperature": 0.0},
        )
        text = resp["output"]["message"]["content"][0]["text"]

        # Parse JSON response
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        parsed = json.loads(text)
        return parsed
    except Exception as e:
        # On error, default to safe (don't block the user experience)
        print(f"[SAFETY] Check failed: {e}")
        return {"safe": True, "note": f"safety check skipped: {e}"}


def _add_branding(img_bytes):
    """Add StoreAI branding overlay to the bottom of a try-on image."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        from io import BytesIO as _BIO
        img = Image.open(_BIO(img_bytes)).convert("RGB")
        w, h = img.size
        sz_lg = max(14, w // 32)
        sz_sm = max(12, w // 38)
        try: font_lg = ImageFont.truetype("/var/task/DejaVuSans.ttf", sz_lg)
        except:
            try: font_lg = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", sz_lg)
            except: font_lg = ImageFont.load_default()
        try: font_sm = ImageFont.truetype("/var/task/DejaVuSans.ttf", sz_sm)
        except:
            try: font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", sz_sm)
            except: font_sm = font_lg

        orange, white, blue = "#ff9900", "#ffffff", "#4db8ff"
        lines = [
            [("Generated at ", font_lg, white), ("StoreAI", font_lg, orange), (" Booth", font_lg, white)],
            [("Powered by ", font_sm, white), ("AWS AI", font_sm, blue), (" Services", font_sm, white)],
        ]

        draw = ImageDraw.Draw(img)
        def sw(segs): return sum(draw.textbbox((0,0),t,font=f)[2]-draw.textbbox((0,0),t,font=f)[0] for t,f,_ in segs)
        def sh(segs): return max(draw.textbbox((0,0),t,font=f)[3]-draw.textbbox((0,0),t,font=f)[1] for t,f,_ in segs)

        lh = [sh(s) for s in lines]
        padding, gap = 12, 5
        banner_h = sum(lh) + gap + padding * 2
        img = img.convert("RGBA")
        overlay = Image.new("RGBA", (w, banner_h), (35, 47, 62, 210))
        img.paste(overlay, (0, h - banner_h), overlay)
        draw = ImageDraw.Draw(img)
        y = h - banner_h + padding
        for i, segs in enumerate(lines):
            x = (w - sw(segs)) // 2
            for text, font, color in segs:
                draw.text((x, y), text, fill=color, font=font)
                x += draw.textbbox((0,0), text, font=font)[2] - draw.textbbox((0,0), text, font=font)[0]
            y += lh[i] + gap
        buf = _BIO()
        img.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return img_bytes


def check_photo(p):
    cid = p.get("customer_id")
    if not cid:
        return {"error": "customer_id required"}
    key = f"photos/{cid}/photo.png"
    try:
        s3.head_object(Bucket=TRYON_BUCKET, Key=key)
        url = s3.generate_presigned_url(
            "get_object", Params={"Bucket": TRYON_BUCKET, "Key": key}, ExpiresIn=3600
        )
        return {"has_photo": True, "url": url}
    except Exception:
        return {"has_photo": False, "url": ""}


def upload_tryon_photo(p):
    cid = p.get("customer_id")
    b64 = p.get("photo_base64")
    if not cid or not b64:
        return {"error": "customer_id and photo_base64 required"}
    # Malformed base64 raises binascii.Error (a ValueError), which without this
    # surfaced as an unhandled Lambda error rather than the {"error": ...} shape the
    # rest of this function returns. validate=True also rejects non-alphabet bytes
    # instead of silently decoding them to garbage that fails further downstream.
    try:
        data = base64.b64decode(b64, validate=True)
    except ValueError:
        return {"error": "photo_base64 is not valid base64"}
    if not data:
        return {"error": "photo_base64 decoded to an empty image"}
    # Bounds what lands in S3 and what the try-on engines will later try to decode.
    # The synchronous path is already capped tighter by Lambda's 6MB request limit
    # (~4.5MB decoded), so this mainly guards direct and queued invokers. It is a
    # size bound only — it is not a decompression-bomb defense, which would need a
    # pixel-count check where the image is actually decoded.
    if len(data) > MAX_PHOTO_BYTES:
        return {"error": f"photo exceeds the {MAX_PHOTO_BYTES // (1024 * 1024)}MB limit"}
    content_type = _sniff_image_type(data)
    if content_type is None:
        return {"error": "photo must be a PNG, JPEG or WebP image"}
    # The key keeps the .png suffix because it is hard-coded in five places across
    # tryon-mcp, size-rec-mcp and the orchestrator; only the declared ContentType is
    # corrected, since it was previously asserted as image/png whatever arrived.
    s3.put_object(
        Bucket=TRYON_BUCKET,
        Key=f"photos/{cid}/photo.png",
        Body=data,
        ContentType=content_type,
    )
    return {"message": "uploaded"}


def virtual_tryon(p):
    """Generate a virtual try-on using a self-hosted engine (FASHN or Qwen
    Image-Edit), or defer to an SQS/Neuron worker when configured."""
    cid = p.get("customer_id")
    pid = p.get("product_id")
    engine = p.get("engine", "qwen_image_edit")
    if not cid or not pid:
        return {"error": "customer_id and product_id required"}

    # Verify photo exists
    photo_key = f"photos/{cid}/photo.png"
    try:
        s3.head_object(Bucket=TRYON_BUCKET, Key=photo_key)
    except Exception:
        return {"error": "no_photo", "message": "I need your photo first to generate a virtual try-on. Say 'take my photo' to open the camera."}

    # Get product image
    prod = product_table.get_item(Key={"product_id": pid}).get("Item", {})
    category = prod.get("category", "mens_tshirt")
    garment_class = CATEGORY_GARMENT_CLASS.get(category, "UPPER_BODY")
    product_image_file = prod.get("image_file", "")

    # FASHN VTON v1.5 (GPU-based, fast)
    if engine == "fashn_vton":
        try:
            import urllib.request
            from io import BytesIO

            photo_bytes = s3.get_object(Bucket=TRYON_BUCKET, Key=photo_key)["Body"].read()
            product_img_bucket = os.environ.get("PRODUCT_IMAGES_BUCKET", TRYON_BUCKET)
            garment_bytes = None
            if product_image_file:
                img_key = f"images/{product_image_file}" if not product_image_file.startswith("images/") else product_image_file
                garment_bytes = s3.get_object(Bucket=product_img_bucket, Key=img_key)["Body"].read()
            elif prod.get("image_url"):
                garment_bytes = urllib.request.urlopen(prod["image_url"], timeout=15).read()
            if not garment_bytes:
                return {"error": f"No image found for product {pid}"}

            # Call FASHN VTON service via multipart POST
            fashn_url = os.environ.get("FASHN_VTON_URL", "http://storeai-fashn-vton:8081")
            boundary = uuid.uuid4().hex
            cat_map = {"UPPER_BODY": "tops", "LOWER_BODY": "bottoms", "FULL_BODY": "one-pieces"}
            fashn_cat = cat_map.get(garment_class, "tops")

            body_parts = []
            for name, fname, data in [("image1", "garment.png", garment_bytes), ("image2", "person.png", photo_bytes)]:
                body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{fname}\"\r\nContent-Type: image/png\r\n\r\n".encode() + data + b"\r\n")
            for name, val in [("category", fashn_cat), ("num_inference_steps", str(p.get("vton_steps", 30))), ("seed", str(p.get("vton_seed", 42)))]:
                body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode())
            body_parts.append(f"--{boundary}--\r\n".encode())
            body_data = b"".join(body_parts)

            req = urllib.request.Request(
                f"{fashn_url}/infer", data=body_data, method="POST",
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            resp = urllib.request.urlopen(req, timeout=120)
            result_bytes = resp.read()
            inference_time = float(resp.headers.get("X-Inference-Time", "0"))

            # Safety check
            bedrock_rt = boto3.client("bedrock-runtime", region_name="us-east-1")
            safety_result = _check_image_safety(result_bytes, garment_class, bedrock_rt)
            if not safety_result["safe"]:
                return {"error": "generation_quality", "message": "Our virtual styling room is warming up for this particular look! The try-on for this item needs a moment to get just right. In the meantime, feel free to try on other items — or come back to this one shortly and we'll have it looking perfect for you!", "blocked": True}

            result_bytes = _add_branding(result_bytes)
            result_key = f"tryon-results/{cid}/{pid}_{int(time.time())}.png"
            s3.put_object(Bucket=TRYON_BUCKET, Key=result_key, Body=result_bytes, ContentType="image/png")
            result_url = s3.generate_presigned_url("get_object", Params={"Bucket": TRYON_BUCKET, "Key": result_key}, ExpiresIn=3600)
            return {"result_url": result_url, "customer_id": cid, "product_id": pid, "engine": "fashn_vton", "inference_sec": inference_time, "cost": {"usd": 0.0}, "safety_checked": True}
        except Exception as e:
            return {"error": f"FASHN VTON failed: {str(e)}"}

    # Qwen-Image-Edit VTON (Neuron / Trainium2, synchronous). Preferred when no SQS worker.
    if engine == "qwen_image_edit" and not VTON_QUEUE_URL:
        try:
            import urllib.request
            photo_bytes = s3.get_object(Bucket=TRYON_BUCKET, Key=photo_key)["Body"].read()
            product_img_bucket = os.environ.get("PRODUCT_IMAGES_BUCKET", TRYON_BUCKET)
            garment_bytes = None
            if product_image_file:
                img_key = f"images/{product_image_file}" if not product_image_file.startswith("images/") else product_image_file
                garment_bytes = s3.get_object(Bucket=product_img_bucket, Key=img_key)["Body"].read()
            elif prod.get("image_url"):
                garment_bytes = urllib.request.urlopen(prod["image_url"], timeout=15).read()
            if not garment_bytes:
                return {"error": f"No image found for product {pid}"}

            # storeai-vton (Trn2) reached via the internal ALB /vton route (Lambda is VPC-attached, can't hit ClusterIP)
            qwen_url = os.environ.get("QWEN_VTON_URL", "http://storeai-vton:8081")
            # Modular, tuned prompt engineering — same vton_engines package as the orchestrator
            # (v1 face-preserving prompt + negative). Caller overrides win if provided.
            from vton_engines import build_request as _build_vton
            _vr = _build_vton(
                "qwen_image_edit", category,
                steps=int(p.get("vton_steps", 50)),
                cfg_override=(float(p["vton_cfg_scale"]) if p.get("vton_cfg_scale")
                              else (float(p["vton_cfg"]) if p.get("vton_cfg") else None)),
                seed=int(p.get("vton_seed", 42)),
            )
            prompt_text = p.get("vton_prompt") or _vr["prompt"]
            neg_prompt = p.get("vton_negative_prompt") or _vr["negative_prompt"]
            _cfg_scale = _vr["true_cfg_scale"]
            _steps = _vr["num_inference_steps"]
            _seed = _vr["seed"]
            boundary = uuid.uuid4().hex
            body_parts = []
            for name, fname, data in [("image1", "garment.png", garment_bytes), ("image2", "person.png", photo_bytes)]:
                body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{fname}\"\r\nContent-Type: image/png\r\n\r\n".encode() + data + b"\r\n")
            for name, val in [("prompt", prompt_text), ("negative_prompt", neg_prompt), ("num_inference_steps", str(_steps)), ("true_cfg_scale", str(_cfg_scale)), ("seed", str(_seed))]:
                body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode())
            body_parts.append(f"--{boundary}--\r\n".encode())

            req = urllib.request.Request(
                f"{qwen_url}/infer", data=b"".join(body_parts), method="POST",
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            resp = urllib.request.urlopen(req, timeout=300)
            result_bytes = resp.read()
            inference_time = float(str(resp.headers.get("X-Inference-Time", "0")).rstrip("s") or "0")

            bedrock_rt = boto3.client("bedrock-runtime", region_name="us-east-1")
            safety_result = _check_image_safety(result_bytes, garment_class, bedrock_rt)
            if not safety_result["safe"]:
                return {"error": "generation_quality", "message": "Our virtual styling room is warming up for this particular look! Please try another item or come back shortly.", "blocked": True}

            result_bytes = _add_branding(result_bytes)
            result_key = f"tryon-results/{cid}/{pid}_{int(time.time())}.png"
            s3.put_object(Bucket=TRYON_BUCKET, Key=result_key, Body=result_bytes, ContentType="image/png")
            result_url = s3.generate_presigned_url("get_object", Params={"Bucket": TRYON_BUCKET, "Key": result_key}, ExpiresIn=3600)
            return {"result_url": result_url, "customer_id": cid, "product_id": pid, "engine": "qwen_image_edit", "inference_sec": inference_time, "cost": {"usd": 0.0}, "safety_checked": True}
        except Exception as e:
            return {"error": f"Qwen VTON failed: {str(e)}"}

    # If SQS queue is configured, defer to Neuron worker (Qwen Image Edit)
    if VTON_QUEUE_URL and engine == "qwen_image_edit":
        job_id = f"JOB-{uuid.uuid4().hex[:8]}"
        job_table.put_item(Item={
            "job_id": job_id,
            "customer_id": cid,
            "product_id": pid,
            "engine": engine,
            "category": category,
            "garment_class": garment_class,
            "status": "queued",
            "created_at": int(time.time()),
            "vton_steps": int(p.get("vton_steps", 50)),
            "vton_seed": int(p.get("vton_seed", 42)),
        })
        sqs.send_message(
            QueueUrl=VTON_QUEUE_URL,
            MessageBody=json.dumps({
                "job_id": job_id, "customer_id": cid, "product_id": pid,
                "engine": engine, "category": category, "garment_class": garment_class,
                "vton_steps": int(p.get("vton_steps", 50)),
                "vton_seed": int(p.get("vton_seed", 42)),
            }),
        )
        return {"deferred": True, "job_id": job_id, "customer_id": cid, "product_id": pid, "message": "Virtual try-on is being generated."}

    # No self-hosted VTON engine matched. Try-on requires a self-hosted engine
    # — FASHN or Qwen Image-Edit — to be deployed and selected.
    return {"error": "no_engine", "message": f"No virtual try-on engine available for '{engine}'. Deploy FASHN or Qwen Image-Edit."}


def tryon_job_status(p):
    """Poll job status from DynamoDB."""
    job_id = p.get("job_id")
    if not job_id:
        return {"error": "job_id required"}
    item = job_table.get_item(Key={"job_id": job_id}).get("Item")
    if not item:
        return {"error": "job not found"}
    result = {"job_id": job_id, "status": item.get("status", "unknown")}
    if item.get("result_url"):
        result["result_url"] = item["result_url"]
    if item.get("error"):
        result["error"] = item["error"]
    return result


def recommend_size(p):
    """Delegates to size-rec-mcp. Kept here for backward compat."""
    func = os.environ.get("SIZE_REC_FUNCTION")
    if func:
        lam = boto3.client("lambda")
        r = lam.invoke(FunctionName=func, Payload=json.dumps(p))
        result = json.loads(r["Payload"].read())
        if result.get("statusCode") == 200:
            return json.loads(result["body"])
    return {"error": "size recommendation not configured"}


def get_tryon_results(p):
    cid = p.get("customer_id", "")
    if not cid:
        return {"error": "customer_id required"}
    prefix = f"tryon-results/{cid}/"
    objs = (
        s3.list_objects_v2(Bucket=TRYON_BUCKET, Prefix=prefix)
        if TRYON_BUCKET
        else {"Contents": []}
    )
    results = []
    for obj in objs.get("Contents", []):
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": TRYON_BUCKET, "Key": obj["Key"]},
            ExpiresIn=3600,
        )
        results.append({"key": obj["Key"], "url": url, "last_modified": str(obj.get("LastModified", ""))})
    results.sort(key=lambda x: x["last_modified"], reverse=True)
    return {"results": results}


TOOLS = {
    "virtual_tryon": virtual_tryon,
    "check_photo": check_photo,
    "upload_tryon_photo": upload_tryon_photo,
    "recommend_size": recommend_size,
    "get_tryon_results": get_tryon_results,
    "tryon_job_status": tryon_job_status,
}


def lambda_handler(event, context):
    body = json.loads(event.get("body", "{}")) if isinstance(event.get("body"), str) else event
    tool = body.get("tool") or body.get("params", {}).get("name", "")
    args = body.get("arguments") or body.get("params", {}).get("arguments", {})
    fn = TOOLS.get(tool)
    if not fn:
        return respond(400, {"error": f"unknown tool: {tool}"})
    return respond(200, fn(args))
