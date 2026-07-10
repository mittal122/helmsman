#!/usr/bin/env bash
# Package Helmsman into ONE portable file you can send to anyone.
# The receiver needs only Docker — no source, no Python, no kubectl/helm/trivy, no internet.
#   ./scripts/package.sh            -> helmsman-image.tar.gz
#   ./scripts/package.sh myrepo:tag -> custom tag
set -euo pipefail
cd "$(dirname "$0")/.."
TAG="${1:-helmsman:1.0}"
OUT="helmsman-image.tar.gz"

echo "==> Building image $TAG (context is lean thanks to .dockerignore)…"
docker build -t "$TAG" .

echo "==> Saving to $OUT …"
docker save "$TAG" | gzip > "$OUT"

echo
echo "✅ Done: $OUT  ($(du -h "$OUT" | cut -f1))"
echo
echo "Send that ONE file. The receiver runs:"
echo "  docker load < $OUT"
echo "  docker run -p 8000:8000 -e ALLOW_OPEN_DEV=1 -e COOKIE_INSECURE=1 \\"
echo "             -v \"\$HOME/.kube:/home/appuser/.kube:ro\" $TAG"
echo "  # then open http://localhost:8000"
