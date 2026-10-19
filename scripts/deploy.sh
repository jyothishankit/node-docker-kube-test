#!/usr/bin/env bash
# Start minikube (if needed), build the images inside it, and deploy everything.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NS=crud-app

if ! minikube status >/dev/null 2>&1; then
  echo "==> Starting minikube"
  minikube start --cpus=4 --memory=4g
fi

echo "==> Enabling metrics-server (required by the HPA)"
minikube addons enable metrics-server

echo "==> Building images inside minikube"
# Builds straight into the cluster's image store (docker or containerd), so no
# second copy of each image is left in the host's Docker.
minikube image build -t crud-backend:latest "$ROOT/backend"
minikube image build -t crud-frontend:latest "$ROOT/frontend"

echo "==> Applying manifests"
kubectl apply -f "$ROOT/k8s/"

# Pick up freshly built images if the deployments already existed.
kubectl -n "$NS" rollout restart deployment/backend deployment/frontend >/dev/null

echo "==> Waiting for rollouts"
kubectl -n "$NS" rollout status deployment/mongo --timeout=180s
kubectl -n "$NS" rollout status deployment/backend --timeout=180s
kubectl -n "$NS" rollout status deployment/frontend --timeout=180s

echo "==> Waiting for the HPA to receive CPU metrics (can take 1-2 min on a fresh cluster)"
for _ in $(seq 1 36); do
  cpu=$(kubectl -n "$NS" get hpa backend \
    -o jsonpath='{.status.currentMetrics[0].resource.current.averageUtilization}' 2>/dev/null || true)
  if [ -n "$cpu" ]; then echo "    HPA sees backend CPU at ${cpu}%"; break; fi
  sleep 5
done
[ -n "${cpu:-}" ] || echo "    WARNING: HPA has no metrics yet; check 'kubectl -n $NS describe hpa backend'"

kubectl -n "$NS" get pods,svc,hpa

cat <<EOF

Done. Open the app / get a URL for the load test with:

  minikube service frontend -n $NS --url

(On macOS with the Docker driver, keep that command running - it holds the tunnel open.)
Watch autoscaling in another terminal with:

  kubectl -n $NS get hpa backend -w
EOF
