#!/usr/bin/env bash
# One-click runner for Helmsman.
#
# Dependency policy: for every tool the app needs, CHECK first.
#   - already on your PC  -> use it, download nothing
#   - missing             -> auto-install it APP-LOCALLY into ./.bin (no sudo, no
#                            system changes; delete ./.bin to undo)
# Only things that can't live in a folder (python3, a container runtime) are
# checked-and-guided instead of auto-installed.
#
#   ./run.sh                 check+install deps, bring up kind, serve on :8000
#   PORT=9000 ./run.sh       different port
#   SKIP_CLUSTER=1 ./run.sh  reuse an existing cluster/kubeconfig (no kind)
#   SKIP_METRICS=1 ./run.sh  don't install metrics-server
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[fatal]\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---- app-local bin (auto-installed tools land here; used before system ones) ----
BIN="$PWD/.bin"; mkdir -p "$BIN"; export PATH="$BIN:$PATH"

# ---- platform ----
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"; case "$ARCH" in x86_64|amd64) ARCH=amd64;; aarch64|arm64) ARCH=arm64;; *) die "unsupported arch: $ARCH";; esac

# resolve a repo's latest release tag via the redirect (no API rate limit, never rots)
gh_latest() { curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$1/releases/latest" | sed -E 's#.*/tag/##'; }

# need <tool> <installer-fn>: use system copy if present, else install app-local
need() {
  local tool="$1" fn="$2"
  if have "$tool"; then ok "$tool — already installed ($(command -v "$tool"))"; return; fi
  say "$tool not found — installing app-local into .bin …"
  "$fn" || die "could not install $tool"
  have "$tool" || die "$tool still not found after install (PATH issue?)"
  ok "$tool — installed to .bin"
}

# ---- installers (write to $BIN, no sudo) ----
i_kubectl() {
  local v; v="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"
  curl -fsSLo "$BIN/kubectl" "https://dl.k8s.io/release/$v/bin/$OS/$ARCH/kubectl"; chmod +x "$BIN/kubectl"
}
i_helm() {
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 \
    | USE_SUDO=false HELM_INSTALL_DIR="$BIN" PATH="$BIN:$PATH" bash >/dev/null
}
i_kind() {
  curl -fsSLo "$BIN/kind" "https://kind.sigs.k8s.io/dl/latest/kind-$OS-$ARCH"; chmod +x "$BIN/kind"
}
i_kubeconform() {
  local t; t="$(gh_latest yannh/kubeconform)"
  curl -fsSL "https://github.com/yannh/kubeconform/releases/download/${t}/kubeconform-${OS}-${ARCH}.tar.gz" \
    | tar -xz -C "$BIN" kubeconform; chmod +x "$BIN/kubeconform"
}
i_kube_score() {
  local t v; t="$(gh_latest zegl/kube-score)"; v="${t#v}"
  curl -fsSL "https://github.com/zegl/kube-score/releases/download/${t}/kube-score_${v}_${OS}_${ARCH}.tar.gz" \
    | tar -xz -C "$BIN" kube-score; chmod +x "$BIN/kube-score"
}
i_trivy() {
  # download the release tarball directly — the get.trivy.dev redirect used by the
  # official install.sh 404s for pinned versions.
  local t v a; t="$(gh_latest aquasecurity/trivy)"; v="${t#v}"
  case "$ARCH" in amd64) a="Linux-64bit";; arm64) a="Linux-ARM64";; *) a="Linux-64bit";; esac
  curl -fsSL "https://github.com/aquasecurity/trivy/releases/download/${t}/trivy_${v}_${a}.tar.gz" \
    | tar -xz -C "$BIN" trivy; chmod +x "$BIN/trivy"
}

# ---- 1. non-isolatable prerequisites: check + guide (never silently install) ----
say "Checking prerequisites"
have curl || die "curl is required to auto-install tools. Install curl, then re-run."
have tar  || die "tar is required to unpack tool downloads. Install tar, then re-run."
have python3 || die "python3 not found. Install Python 3.12+ (e.g. 'sudo apt install python3 python3-venv'), then re-run."
if have docker; then
  docker info >/dev/null 2>&1 || die "docker is installed but the daemon isn't running. Start Docker, then re-run."
  ok "container runtime — docker"
elif have podman; then
  export KIND_EXPERIMENTAL_PROVIDER=podman; ok "container runtime — podman"
else
  die "no container runtime found. kind needs Docker or Podman. Install one (https://docs.docker.com/engine/install/), then re-run."
fi

# ---- 2. CLI tools: use system copy if present, else auto-install app-local ----
say "Resolving CLI tools (install only what's missing)"
need kubectl     i_kubectl
need helm        i_helm
need kind        i_kind
need kubeconform i_kubeconform
need kube-score  i_kube_score
# trivy is optional — the image scan degrades gracefully if it can't be installed
if have trivy; then ok "trivy — already installed ($(command -v trivy))"
else say "trivy not found — installing app-local (optional) …"; i_trivy && ok "trivy — installed to .bin" || warn "trivy install failed — image scan will be skipped (not a pass)"; fi

# ---- 3. python venv + backend deps (install only if missing) ----
if [ ! -d .venv ]; then say "Creating venv (.venv)"; python3 -m venv .venv; fi
# shellcheck disable=SC1091
source .venv/bin/activate
if ! python -c "import fastapi, cryptography, anthropic" >/dev/null 2>&1; then
  say "Installing backend deps"; pip install -q -r backend/requirements.txt
else ok "backend Python deps — already installed"; fi

# ---- 4. kind cluster (idempotent) ----
if [ "${SKIP_CLUSTER:-0}" = "1" ]; then
  warn "SKIP_CLUSTER=1 — using ambient kubeconfig, not creating a kind cluster"
else
  say "Ensuring kind cluster 'helmsman'"
  kind get clusters 2>/dev/null | grep -qx helmsman || kind create cluster --name helmsman
  kubectl cluster-info --context kind-helmsman >/dev/null && ok "cluster ready (kind-helmsman)"
fi

# ---- 5. metrics-server (best-effort; needed for HPA + kubectl top) ----
if [ "${SKIP_METRICS:-0}" = "1" ]; then warn "SKIP_METRICS=1 — skipping metrics-server"
elif kubectl -n kube-system get deploy metrics-server >/dev/null 2>&1; then ok "metrics-server — already present"
else say "Installing metrics-server"; scripts/monitoring-up.sh || warn "metrics-server install failed — continuing without it"; fi

# ---- 6. config hints (ambient-kind deploys need neither) ----
[ -z "${AUTH_TOKEN:-}" ]          && warn "AUTH_TOKEN unset — API is OPEN (fine for local; set it to require a token)"
[ -z "${KUBECONFIG_ENC_KEY:-}" ]  && warn "KUBECONFIG_ENC_KEY unset — /kubeconfigs store disabled; deploys use the ambient kind cluster"

# ---- 7. serve ----
say "Backend live at http://localhost:${PORT}   (Ctrl-C to stop)"
cd backend
exec uvicorn main:app --port "$PORT"
