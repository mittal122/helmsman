#!/usr/bin/env bash
# Package Helmsman into ONE portable file you can send to anyone.
# The receiver needs only Docker — no source, no Python, no kubectl/helm/trivy, no internet.
#   ./scripts/package.sh                          -> tar for THIS machine's CPU arch
#   ./scripts/package.sh helmsman:1.0 linux/arm64 -> tar for Apple Silicon / ARM (cross-built)
#   ./scripts/package.sh myrepo:tag               -> custom tag
set -euo pipefail
cd "$(dirname "$0")/.."
TAG="${1:-helmsman:1.0}"
PLATFORM="${2:-}"
OUT="helmsman-image.tar.gz"

if [ -n "$PLATFORM" ]; then
  echo "==> Cross-building $TAG for $PLATFORM (enabling emulation)…"
  docker run --privileged --rm tonistiigi/binfmt --install all >/dev/null 2>&1 || true
  docker buildx build --platform "$PLATFORM" -t "$TAG" --load .
else
  echo "==> Building image $TAG for this machine ($(uname -m))…"
  docker build -t "$TAG" .
fi

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
