#!/usr/bin/env bash
set -euo pipefail
# metrics-server for kubectl top. On kind, kubelet serving certs aren't signed
# by the cluster CA, so metrics-server needs --kubelet-insecure-tls.
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl patch -n kube-system deployment metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
kubectl -n kube-system rollout status deployment metrics-server --timeout=120s
echo "metrics-server ready"
