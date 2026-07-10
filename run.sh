#!/usr/bin/env bash
# One-shot runner for Helmsman: preflight -> venv/deps -> kind cluster ->
# metrics-server -> start backend. Reuses scripts/*.sh; nothing duplicated.
#
#   ./run.sh                 full bring-up + serve on :8000
#   PORT=9000 ./run.sh       serve on a different port
#   SKIP_CLUSTER=1 ./run.sh  reuse an existing cluster/kubeconfig (no kind)
#   SKIP_METRICS=1 ./run.sh  don't install metrics-server (HPA/top disabled)
#
# Optional env the backend reads (see CLAUDE.md): AUTH_TOKEN (enables auth),
# KUBECONFIG_ENC_KEY (enables the encrypted kubeconfig store — a Fernet key).
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[fatal]\033[0m %s\n' "$*" >&2; exit 1; }

# 1. Preflight — hard-require what a deploy cannot work without.
say "Preflight tool check"
for t in python3 kind kubectl helm kubeconform kube-score; do
  command -v "$t" >/dev/null 2>&1 || die "missing required tool: $t (install it, then re-run)"
done
# Optional — degrade gracefully, don't fail.
command -v trivy >/dev/null 2>&1 || warn "trivy not installed — image scan gate will skip (run scripts/install-trivy.sh to enable)"

# 2. venv + deps (create once; reuse thereafter).
if [ ! -d .venv ]; then
  say "Creating venv (.venv)"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
if ! python -c "import fastapi, cryptography, anthropic" >/dev/null 2>&1; then
  say "Installing backend deps"
  pip install -q -r backend/requirements.txt
fi

# 3. Cluster (kind) — idempotent; kind-up.sh no-ops if it already exists.
if [ "${SKIP_CLUSTER:-0}" = "1" ]; then
  warn "SKIP_CLUSTER=1 — using ambient kubeconfig, not creating a kind cluster"
else
  say "Bringing up kind cluster"
  scripts/kind-up.sh
fi

# 4. metrics-server — needed for HPA + kubectl top; best-effort.
if [ "${SKIP_METRICS:-0}" = "1" ]; then
  warn "SKIP_METRICS=1 — skipping metrics-server (HPA autodetect off, no kubectl top)"
elif kubectl -n kube-system get deploy metrics-server >/dev/null 2>&1; then
  say "metrics-server already present"
else
  say "Installing metrics-server"
  scripts/monitoring-up.sh || warn "metrics-server install failed — continuing without it"
fi

# 5. Config hints (non-fatal — ambient-kind deploys need neither).
[ -z "${AUTH_TOKEN:-}" ] && warn "AUTH_TOKEN unset — API is OPEN (fine for local; set it to require a token)"
[ -z "${KUBECONFIG_ENC_KEY:-}" ] && warn "KUBECONFIG_ENC_KEY unset — /kubeconfigs store disabled; deploys use the ambient kind cluster"

# 6. Serve. Foreground so Ctrl-C stops it.
say "Backend live at http://localhost:${PORT}  (Ctrl-C to stop)"
cd backend
exec uvicorn main:app --port "$PORT"
