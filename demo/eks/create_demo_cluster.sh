#!/usr/bin/env bash
# create_demo_cluster.sh — Final Demo B 용 EKS 클러스터 생성 + ArgoCD + Prom + Loki 설치.
#
# 설계서 ADR-009: 실제 EKS. k3d / kind 로 타협하지 않는다.
# 본 스크립트는 데모 직전 1회 실행하고 데모 종료 직후 destroy_demo_cluster.sh 로 정리한다.
#
# 사전 도구: eksctl, aws-cli, kubectl, helm
#
# 사용:
#   ./demo/eks/create_demo_cluster.sh [region]
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_FILE="${HERE}/eksctl-cluster.yaml"
REGION="${1:-ap-northeast-2}"
CLUSTER_NAME="recoder-demo"

log() { printf "\033[1;36m[demo]\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31m[demo:ERROR]\033[0m %s\n" "$*" >&2; exit 1; }

# 0) 사전 점검
for cmd in eksctl aws kubectl helm; do
  command -v "$cmd" >/dev/null 2>&1 || fail "$cmd is required"
done

aws sts get-caller-identity >/dev/null 2>&1 \
  || fail "AWS credentials not configured (aws sts get-caller-identity failed)"

# 1) 비용 가드 — 기존 클러스터가 살아있다면 중복 생성 금지
if eksctl get cluster --region "$REGION" --name "$CLUSTER_NAME" >/dev/null 2>&1; then
  log "cluster '$CLUSTER_NAME' already exists. skipping create."
else
  log "creating EKS cluster ($REGION) — ~15 minutes"
  eksctl create cluster -f "$CLUSTER_FILE"
fi

# 2) kubeconfig
log "updating kubeconfig"
aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER_NAME"

# 3) ArgoCD 설치 (가장 가벼운 manifest)
log "installing ArgoCD"
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# 4) Prometheus + Loki (helm) — 데모 시연용 최소 설치
log "adding helm repos"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo update >/dev/null

kubectl create namespace observability --dry-run=client -o yaml | kubectl apply -f -

log "installing kube-prometheus-stack (minimal)"
helm upgrade --install kps prometheus-community/kube-prometheus-stack \
  --namespace observability \
  --set grafana.enabled=false \
  --set alertmanager.enabled=false \
  --set prometheus.prometheusSpec.retention=1d \
  --set prometheus.prometheusSpec.resources.requests.cpu=100m \
  --set prometheus.prometheusSpec.resources.requests.memory=256Mi

log "installing loki (single-binary)"
helm upgrade --install loki grafana/loki \
  --namespace observability \
  --set deploymentMode=SingleBinary \
  --set loki.auth_enabled=false \
  --set loki.storage.type=filesystem \
  --set "singleBinary.replicas=1" \
  --set "singleBinary.persistence.enabled=false" \
  --set test.enabled=false \
  --set monitoring.selfMonitoring.enabled=false \
  --set monitoring.lokiCanary.enabled=false \
  --set chunksCache.enabled=false \
  --set resultsCache.enabled=false

# 5) ArgoCD admin password 출력
log "waiting for ArgoCD secret"
for i in $(seq 1 30); do
  if kubectl -n argocd get secret argocd-initial-admin-secret >/dev/null 2>&1; then
    break
  fi
  sleep 4
done

ARGO_PW="$(kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' 2>/dev/null | base64 --decode || true)"

cat <<EOF

============================================================
ReCoder Final Demo B — EKS cluster '$CLUSTER_NAME' is READY.
Region          : $REGION
ArgoCD UI       : kubectl -n argocd port-forward svc/argocd-server 8080:443
ArgoCD admin pw : ${ARGO_PW:-<not ready yet — re-run secret read>}
Prometheus      : kubectl -n observability port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090
Loki gateway    : kubectl -n observability port-forward svc/loki 3100:3100

DON'T FORGET: run destroy_demo_cluster.sh after the demo.
============================================================
EOF
