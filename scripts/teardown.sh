#!/usr/bin/env bash
# Remove the app from the cluster (keeps minikube itself running).
set -euo pipefail
kubectl delete namespace crud-app --ignore-not-found
