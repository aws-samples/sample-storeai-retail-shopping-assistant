#!/usr/bin/env bash
# scripts/rotate-capacity-block.sh — rotate the Trn2 Neuron Capacity Block reservation.
#
# Capacity Blocks expire and are relaunched over time. This helper is the one command to point
# StoreAI at a freshly launched CB: it updates SSM (the source of truth the deploy CLI bridges to
# a Terraform var), the deploy config, scales the existing Neuron MNG to 0 (required before a CB
# node group can change its reservation), and rebuilds the Neuron MNG via `storeai up --module eks`.
# The compiled model artifacts persist in the neuron-cache S3 bucket, so serving comes back fast.
#
# Usage:
#   ./scripts/rotate-capacity-block.sh cr-0abc123def456 [availability-zone]
#   ENV=dev AWS_REGION=us-east-2 ./scripts/rotate-capacity-block.sh cr-0abc123def456 us-east-2b
set -euo pipefail

CB_ID="${1:?usage: rotate-capacity-block.sh <cr-reservation-id> [availability-zone]}"
AZ="${2:-}"
ENV="${ENV:-${STOREAI_ENV:-dev}}"
REGION="${AWS_REGION:-us-east-2}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="${ROOT_DIR}/deploy/config/defaults.json"
SSM_NAME="/storeai-${ENV}/neuron/capacity_block_reservation_id"
CLUSTER="storeai-${ENV}"
NG="storeai-${ENV}-neuron-cb"

echo "Cluster: ${CLUSTER} | Region: ${REGION} | CB: ${CB_ID}${AZ:+ | AZ: ${AZ}}"

# 1. SSM — the source of truth the deploy CLI (deploy/lib/tf.sh) bridges into -var capacity_block_reservation_id.
aws ssm put-parameter --name "$SSM_NAME" --value "$CB_ID" --type String --overwrite \
  --region "$REGION" --query 'Version' --output text >/dev/null
echo "✅ SSM ${SSM_NAME} updated"

# 2. Deploy config — resolver prereq (prerequisites.capacityBlock.reservationId) + subnet AZ + enable neuron modules.
python3 - "$CONFIG" "$CB_ID" "$AZ" <<'PY'
import json, sys, collections
path, cbid, az = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(path), object_pairs_hook=collections.OrderedDict)
# reservationId lives under prerequisites.capacityBlock — that is what
# deploy/lib/tf.sh and resolve.py read, and the schema root is
# additionalProperties:false, so a top-level key fails `storeai up` preflight.
d.setdefault("prerequisites", {}).setdefault("capacityBlock", {})["reservationId"] = cbid
if az:
    d.setdefault("global", {})["capacityBlockAz"] = az
for m in ("llm", "image-edit-model"):
    d.setdefault("modules", {}).setdefault(m, {})["enabled"] = True
json.dump(d, open(path, "w"), indent=2); open(path, "a").write("\n")
print("✅ config updated (prerequisites.capacityBlock.reservationId, capacityBlockAz, modules llm+image-edit-model enabled)")
PY

# 3. Scale the existing Neuron MNG to 0 (a CB node group must be at 0 before its reservation changes).
DESIRED=$(aws eks describe-nodegroup --cluster-name "$CLUSTER" --nodegroup-name "$NG" \
  --region "$REGION" --query 'nodegroup.scalingConfig.desiredSize' --output text 2>/dev/null || echo "0")
if [ "$DESIRED" != "0" ] && [ "$DESIRED" != "None" ]; then
  echo "⏳ scaling ${NG} to 0 before reservation change..."
  aws eks update-nodegroup-config --cluster-name "$CLUSTER" --nodegroup-name "$NG" \
    --scaling-config minSize=0,maxSize=1,desiredSize=0 --region "$REGION" --query 'update.status' --output text
  aws eks wait nodegroup-active --cluster-name "$CLUSTER" --nodegroup-name "$NG" --region "$REGION" 2>/dev/null || true
fi

# 4. Rebuild the Neuron MNG with the new CB via Terraform (deploy CLI reads the SSM value).
echo "== rebuilding Neuron MNG (terraform via deploy CLI) =="
( cd "$ROOT_DIR/deploy" && ./storeai up --module eks --env "$ENV" --region "$REGION" --yes )

echo
echo "✅ Capacity Block rotated to ${CB_ID}."
echo "   Redeploy Neuron workloads (served from the S3 compile cache):"
echo "     cd deploy && ./storeai up --module llm --module image-edit-model --yes"
