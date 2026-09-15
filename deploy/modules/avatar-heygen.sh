#!/usr/bin/env bash
# avatar-heygen (L2) — LiveAvatar relay. Build arm64 → ECR storeai-dev-liveavatar,
# inject the API key from SSM (SecureString), deploy as service liveavatar-svc:8502.
set -euo pipefail

NS="default"
_r() { python3 "${LIB_DIR}/resolve.py" --config "$1" get "$2" 2>/dev/null; }

mod_deploy() {
  local config="$1" region env account repo tag ecr_uri src manifest key
  region=$(_r "$config" global.region); region="${region:-us-east-2}"
  env=$(_r "$config" global.env); env="${env:-dev}"
  account=$(_r "$config" global.accountId)
  if [ -z "$account" ] || [ "$account" = "auto" ] || [ "$account" = "None" ]; then
    account=$(aws sts get-caller-identity --query Account --output text)
  fi

  # HeyGen API key: config is the source of truth (D-013). Fall back to SSM only if config is empty.
  key=$(_r "$config" prerequisites.avatar.apiKey)
  { [ -z "${key:-}" ] || [ "$key" = "None" ]; } && key=$(aws ssm get-parameter --name "/storeai-${env}/avatar/api-key" --with-decryption --region "$region" --query 'Parameter.Value' --output text 2>/dev/null || true)
  if [ -z "${key:-}" ] || [ "$key" = "None" ]; then
    echo "  ERROR: HeyGen API key not set. Provide prerequisites.avatar.apiKey in the config (or /storeai-${env}/avatar/api-key in SSM)." >&2
    return 1
  fi

  src="${PROJECT_DIR}/components/avatar"
  repo="storeai-${env}-liveavatar"
  tag="$(cd "$PROJECT_DIR" && git rev-parse --short HEAD 2>/dev/null || echo dev)-$(date +%H%M%S)"
  ecr_uri="${account}.dkr.ecr.${region}.amazonaws.com/${repo}"

  echo "  building avatar image (arm64) → ${ecr_uri}:${tag}"
  aws ecr get-login-password --region "$region" | podman login --username AWS --password-stdin "${account}.dkr.ecr.${region}.amazonaws.com" >/dev/null
  podman build --platform linux/arm64 -t "${ecr_uri}:${tag}" -t "${ecr_uri}:latest" "$src"
  podman push "${ecr_uri}:${tag}"; podman push "${ecr_uri}:latest"

  export REGION="$region" ENV="$env" ACCOUNT_ID="$account" IMAGE_TAG="$tag"

  # HeyGen credential → k8s Secret, referenced by the Deployment via secretKeyRef.
  # The key is no longer substituted into the manifest, so it does not appear in the
  # rendered temp file or in `kubectl get deployment -o yaml`. Idempotent (create-or-update).
  kubectl create secret generic storeai-avatar-secrets -n "$NS" \
    --from-literal=liveavatar-api-key="$key" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null

  manifest="$(mktemp -t avatar.XXXXXX.yaml)"
  envsubst < "${PROJECT_DIR}/k8s/avatar-serving.yaml" > "$manifest"
  kubectl apply -f "$manifest"; rm -f "$manifest"
  kubectl rollout status deployment/storeai-avatar -n "$NS" --timeout=300s
  echo "  avatar service: http://liveavatar-svc.${NS}.svc.cluster.local:8502 (/avatar,/token,/ws)"
}

mod_teardown() {
  # TYPE/NAME per object: `kubectl delete TYPE NAME...` takes a single type, so the bare
  # form treated "service" and "liveavatar-svc" as deployment names and left the Service
  # behind. The comma form other modules use does not fit here — the Deployment and the
  # Service have different names (k8s/avatar-serving.yaml).
  kubectl delete deployment/storeai-avatar service/liveavatar-svc -n "$NS" --ignore-not-found >/dev/null 2>&1 || true
  kubectl delete secret storeai-avatar-secrets -n "$NS" --ignore-not-found >/dev/null 2>&1 || true
}
mod_verify() { kubectl get pods -n "$NS" -l app=storeai-avatar 2>/dev/null || true; }
