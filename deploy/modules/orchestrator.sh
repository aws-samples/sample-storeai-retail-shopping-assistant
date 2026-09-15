#!/usr/bin/env bash
# orchestrator (L2) — LangGraph chat brain. Builds the arm64 image, pushes to
# ECR, and deploys to EKS. All LLM calls route through the LiteLLM gateway (D-010).
set -euo pipefail

ORCH_NS="default"
SRC_DIR_DEFAULT="components/orchestrator"

mod_deploy() {
  local config="$1" region env account replicas repo tag ecr_uri mkey src
  region=$(python3 "${LIB_DIR}/resolve.py" --config "$config" get global.region); region="${region:-us-east-2}"
  env=$(python3 "${LIB_DIR}/resolve.py" --config "$config" get global.env); env="${env:-dev}"
  replicas=$(python3 "${LIB_DIR}/resolve.py" --config "$config" get modules.orchestrator.replicas); replicas="${replicas:-2}"

  account=$(python3 "${LIB_DIR}/resolve.py" --config "$config" get global.accountId)
  if [ -z "$account" ] || [ "$account" = "auto" ] || [ "$account" = "None" ]; then
    account=$(aws sts get-caller-identity --query Account --output text)
  fi

  src="${PROJECT_DIR}/${SRC_DIR_DEFAULT}"
  [ -d "$src" ] || src="${PROJECT_DIR}/orchestrator"   # pre-restructure fallback
  repo="storeai-${env}-orchestrator"
  tag="$(cd "$PROJECT_DIR" && git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"
  # If the working tree has uncommitted changes, the SHA alone is stale: rebuilding
  # under the same tag makes `kubectl apply` a no-op (no rollout, old image kept).
  # Append a timestamp so every dirty-tree deploy ships a fresh, rolled-out image.
  if ! (cd "$PROJECT_DIR" && git diff --quiet HEAD 2>/dev/null && git diff --quiet --cached 2>/dev/null); then
    tag="${tag}-$(date +%s)"
  fi
  ecr_uri="${account}.dkr.ecr.${region}.amazonaws.com/${repo}"

  echo "  building orchestrator image (arm64) → ${ecr_uri}:${tag}"
  aws ecr get-login-password --region "$region" | podman login --username AWS --password-stdin "${account}.dkr.ecr.${region}.amazonaws.com" >/dev/null
  podman build --platform linux/arm64 -t "${ecr_uri}:${tag}" -t "${ecr_uri}:latest" "$src"
  podman push "${ecr_uri}:${tag}"
  podman push "${ecr_uri}:latest"

  # Master key for the gateway (created by the litellm-gateway module).
  mkey=$(aws ssm get-parameter --name "/storeai-${env}/litellm/master-key" --with-decryption --region "$region" --query 'Parameter.Value' --output text 2>/dev/null || true)
  if [ -z "$mkey" ] || [ "$mkey" = "None" ]; then
    echo "  ERROR: litellm master key not found in SSM — deploy litellm-gateway first." >&2
    return 1
  fi

  local cog_pool cog_client
  cog_pool=$(terraform -chdir="${PROJECT_DIR}/infra/terraform" output -raw cognito_user_pool_id 2>/dev/null || true)
  cog_client=$(terraform -chdir="${PROJECT_DIR}/infra/terraform" output -raw cognito_client_id 2>/dev/null || true)

  # Fail fast on empty Cognito outputs. Deploying with blank pool/client ids brings the
  # orchestrator up with no auth config, so every authenticated request fails at runtime
  # with an error that points here only after a long detour. Stop now with a clear message.
  if [ -z "$cog_pool" ] || [ "$cog_pool" = "None" ] || [ -z "$cog_client" ] || [ "$cog_client" = "None" ]; then
    echo "  ERROR: Cognito outputs not found (cognito_user_pool_id / cognito_client_id) — deploy the cognito-user module first." >&2
    return 1
  fi

  # Public base URL for shareable links (e.g. the try-on QR gallery). Use the CDN's
  # app_url output — storeai.<custom_domain> when a domain is set, else the CloudFront
  # URL — so the QR always points where the app is actually served (D-031/D-044).
  # Never the bare custom domain (users bring existing domains).
  local custom_domain public_base cf_domain
  public_base=$(terraform -chdir="${PROJECT_DIR}/infra/terraform" output -raw app_url 2>/dev/null || true)
  if [ -z "$public_base" ] || [ "$public_base" = "None" ]; then
    custom_domain=$(python3 "${LIB_DIR}/resolve.py" --config "$config" get global.customDomain 2>/dev/null)
    if [ -n "$custom_domain" ] && [ "$custom_domain" != "None" ] && [ "$custom_domain" != "auto" ]; then
      public_base="https://storeai.${custom_domain}"
    else
      cf_domain=$(terraform -chdir="${PROJECT_DIR}/infra/terraform" output -raw cloudfront_domain_name 2>/dev/null || true)
      { [ -n "$cf_domain" ] && [ "$cf_domain" != "None" ]; } && public_base="https://${cf_domain}" || public_base=""
    fi
  fi

  export REGION="$region" ENV="$env" ACCOUNT_ID="$account" ORCH_REPLICAS="$replicas" IMAGE_TAG="$tag" LITELLM_MASTER_KEY="$mkey" COGNITO_USER_POOL_ID="${cog_pool}" COGNITO_APP_CLIENT_ID="${cog_client}" VTON_SAFETY_ENABLED="${VTON_SAFETY_ENABLED:-true}" PUBLIC_BASE_URL="${public_base}"
  local manifest
  manifest="$(mktemp -t orchestrator.XXXXXX.yaml)"
  envsubst < "${PROJECT_DIR}/k8s/orchestrator-serving.yaml" > "$manifest"
  kubectl apply -f "$manifest"
  rm -f "$manifest"

  echo "  waiting for orchestrator rollout..."
  kubectl rollout status deployment/storeai-orchestrator -n "$ORCH_NS" --timeout=300s
  echo "  orchestrator service: http://storeai-orchestrator.${ORCH_NS}.svc.cluster.local:8080"
}

mod_teardown() {
  kubectl delete -f "${PROJECT_DIR}/k8s/orchestrator-serving.yaml" --ignore-not-found >/dev/null 2>&1 || \
    kubectl delete deployment,service storeai-orchestrator -n "$ORCH_NS" --ignore-not-found >/dev/null 2>&1 || true
}

mod_verify() {
  kubectl get pods -n "$ORCH_NS" -l app=storeai-orchestrator 2>/dev/null || true
}
