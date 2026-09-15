# Troubleshooting

Common problems and how to resolve them. Each entry lists the symptom, the likely cause, and the fix.
For deployment concepts, see [getting-started.md](getting-started.md).

## Deployment

### Preflight fails: a required tool is missing
**Cause:** One of the required CLIs is not installed or not on your `PATH`.
**Fix:** Install the missing tool (see the prerequisites in [getting-started.md](getting-started.md))
and re-run `./deploy/storeai up`. Preflight is idempotent.

### `terraform init` fails on the backend
**Cause:** The Terraform state bucket does not exist, or its region does not match
`global.stateBucketRegion`. The deployer validates but never creates the bucket (D-013).
**Fix:** Create the bucket and set `global.tfStateBucket` and `global.stateBucketRegion` correctly.
The backend region can differ from the deployment region (D-024).

### A module fails with "missing prerequisite"
**Cause:** An enabled module needs an input that is not set — for example `avatar-heygen` with no API
key, or a Neuron model with no capacity block.
**Fix:** Supply the prerequisite in `storeai.config.json`, or disable the module. The deployer fails
fast here before making AWS changes.

### CloudFront returns errors right after deploy
**Cause:** The CDN was applied before the cluster ingress ALB existed.
**Fix:** StoreAI applies Terraform in two passes to avoid this (D-041). If you ran a partial or manual
apply, re-run `./deploy/storeai up` so the post-cluster pass resolves the ALB and re-applies the CDN.

### A direct image build fails with `app/vton_engines/ is missing from the build context`
**Cause:** The shared VTON prompt-engineering package lives at `components/shared/vton_engines/` and is
copied into each consumer's build context by `scripts/sync-vton-engines.sh`. Those copies are
gitignored, so a fresh clone does not have them. `./deploy/storeai up` runs the sync for you; building
a component image directly (`docker build components/orchestrator`) does not.
**Fix:** Run `./scripts/sync-vton-engines.sh` from the repo root, then rebuild. Edit only the canonical
copy under `components/shared/vton_engines/` — the synced copies are overwritten on every sync.

## Runtime

### Pods stuck in `Pending`
**Cause:** No node has capacity yet, or accelerator node groups scaled to zero.
**Fix:** For general services on Auto Mode, nodes provision automatically within a few minutes. For
GPU or Trainium workloads, confirm the managed node group has capacity and, for Trainium, that the
capacity block reservation is active and in the expected Availability Zone.

### The shop shows no products
**Cause:** The catalog was not seeded, or the orchestrator cannot invoke the MCP Lambdas.
**Fix:** Confirm `seed-data` ran (`./deploy/storeai status`). If products exist in DynamoDB but the UI
is empty, check that the orchestrator's IAM role can invoke the MCP Lambda functions.

### Chat or voice fails with `AccessDeniedException` (model not accessible)
**Cause:** Bedrock model access has not been granted for the account/region, so calls to Anthropic
Claude or Amazon Nova are denied.
**Fix:** In the AWS console, open **Bedrock → Model access** and enable the required models (Anthropic
Claude and Amazon Nova) in the deployment region. Model access is not automatic.

### Product images do not load
**Cause:** Product images are served over the public `/images/*` path. If they 401, the frontend is
requesting them through the authenticated `/api/*` path instead.
**Fix:** Product images must load from `/images/...`, not `/api/images/...`. Customer try-on images use
the authenticated `/tryon-images/*` and `/tryon-results/*` paths with a token.

### Virtual try-on returns a content-review message
**Cause:** The generated image was flagged by the content-safety check, and the FASHN fallback either
was also flagged or errored.
**Fix:** This is expected behavior for flagged output — try a different product or photo. If you need
to disable verification (for example, in a controlled test), set `VTON_SAFETY_ENABLED=false` on the
orchestrator. Note that if the verifier model itself is unreachable, verification fails open (the image
is returned unchecked) rather than blocking the try-on.

### Voice input does not work
**Cause:** The selected speech-to-text engine is not deployed, or the browser could not open the
WebSocket.
**Fix:** Confirm the chosen engine is enabled (`voice-nova` for Nova Sonic, `whisper` for Whisper).
WebSocket routes (`/stt`, `/tts`, `/whisper`) are authenticated with a token passed as a query
parameter because browsers cannot set WebSocket headers; confirm you are signed in.

### Requests to `/api/*` return 401
**Cause:** Authentication is enabled and the request has no valid Cognito token.
**Fix:** Sign in to obtain a token. Authentication is environment-gated by `COGNITO_USER_POOL_ID`; if
it is set, the edge filter and the application both require a valid token (public paths such as
`/images` and `/health` are excluded). See [design-decisions.md](design-decisions.md) (D-042).

### QR / share link is not reachable
**Cause:** The try-on QR encodes `${PUBLIC_BASE_URL}/api/tryon-share?token=…`. `PUBLIC_BASE_URL` is
`https://storeai.<customDomain>` when a custom domain is configured, otherwise the CloudFront URL.
With a custom domain the app is auto-wired to `storeai.<customDomain>` (us-east-1 ACM cert +
CloudFront alias + Route53 CNAME), but a freshly provisioned distribution + DNS can take ~10–20
minutes to propagate — during which the link may not resolve yet.
**Fix:** Allow a few minutes after deploy for CloudFront/DNS propagation, then confirm the base URL
the orchestrator baked in and that the domain serves:
```bash
kubectl get deploy storeai-orchestrator -n default \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="PUBLIC_BASE_URL")].value}'
curl -sI https://storeai.<customDomain>/ | head -1
```
With no custom domain, the QR uses the CloudFront URL and works immediately. See
[configuration.md](configuration.md) (`customDomain`).

## Observability

### Verify which model actually served a chat request

The gateway logs the exact backend model for every call — use this to confirm a UI model
selection is honored end to end:

```bash
kubectl logs deploy/litellm -n default | grep "acompletion(model="
# litellm.acompletion(model=bedrock/us.anthropic.claude-sonnet-4-6) 200 OK
# litellm.acompletion(model=hosted_vllm/qwen3-8b) 200 OK
```

For the self-hosted Qwen3 pod specifically (confirms inference actually hit Trainium):

```bash
kubectl logs deploy/storeai-llm -c vllm -n default | grep "chat/completions"
```

### LiteLLM dashboard

LiteLLM ships a built-in admin UI at `/ui`. Reach it with a port-forward:

```bash
kubectl port-forward svc/litellm 4000:4000 -n default
# then open http://localhost:4000/ui and log in with the master key
# (stored in SSM at /storeai-<env>/litellm/master-key)
```

The UI loads (HTTP 200), but StoreAI runs the gateway **DB-less** by design — per-call cost is
aggregated app-side, which avoids a deprecated bundled database image. Without the database, the
dashboard's spend / virtual-keys / request-log pages are empty or limited; use the pod logs above
for request-level observability. To get the full dashboard, deploy the chart with its database
enabled (`db.deployStandalone=true`).

### A chat try-on "completes" instantly but shows no image

Virtual try-on runs as an LLM tool call (`virtual_tryon`). Strong models (Claude Sonnet 4.6)
reliably call the tool; weaker models (Claude Haiku, self-hosted Qwen3-8B) sometimes claim success
in text **without** calling the tool — so no image is generated and the reply returns in ~1–2s
instead of the ~15–40s a real generation takes. This is model tool-calling behavior, not a try-on
failure (the engines and the `/virtual_try_on` path work independently). Switch the chat model to
Claude Sonnet for reliable agentic try-on, or restate the request explicitly.

## Teardown

### `down --all` stops partway
**Cause:** AWS releases some resources slowly — most commonly VPC-attached Lambda network interfaces,
which can hold up security-group and subnet deletion for several minutes.
**Fix:** This is normal AWS behavior, not a failure. Re-run `./deploy/storeai down --all`; Terraform is
idempotent and completes the remaining deletions. Your Terraform state bucket is always preserved.
