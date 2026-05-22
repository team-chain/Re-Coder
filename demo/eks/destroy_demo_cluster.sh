#!/usr/bin/env bash
# destroy_demo_cluster.sh — 데모 종료 즉시 정리 (비용 0 확인).
#
# 설계서 ADR-009: 데모 완료 즉시 삭제하는 스크립트를 미리 준비한다.
#
# 사용:
#   ./demo/eks/destroy_demo_cluster.sh [region]
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_FILE="${HERE}/eksctl-cluster.yaml"
REGION="${1:-ap-northeast-2}"
CLUSTER_NAME="recoder-demo"

log() { printf "\033[1;33m[demo]\033[0m %s\n" "$*"; }

command -v eksctl >/dev/null 2>&1 || { echo "eksctl not found"; exit 1; }

if ! eksctl get cluster --region "$REGION" --name "$CLUSTER_NAME" >/dev/null 2>&1; then
  log "cluster '$CLUSTER_NAME' not found. nothing to delete."
  exit 0
fi

log "deleting EKS cluster '$CLUSTER_NAME' ($REGION) — ~10 minutes"
eksctl delete cluster -f "$CLUSTER_FILE" --disable-nodegroup-eviction --wait

cat <<EOF

============================================================
ReCoder Final Demo B — cluster '$CLUSTER_NAME' DESTROYED.
Cost guard: confirm in AWS console that no NAT GW / NLB lingers.
  aws ec2 describe-nat-gateways --region $REGION
  aws elbv2 describe-load-balancers --region $REGION
============================================================
EOF
