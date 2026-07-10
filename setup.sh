#!/usr/bin/env bash
# Zero-to-running bootstrap for Linux / macOS / WSL2.
# Installs ONLY what's missing (Docker, Python), then hands off to ./run.sh which
# installs the Kubernetes CLIs, brings up a local cluster, and serves the app.
# Everything is idempotent: present -> skipped, missing -> installed.
set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[fatal]\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

OS="$(uname -s)"

# ---- 1. Docker (container runtime + local cluster backend) ----
if have docker; then
  ok "Docker present ($(command -v docker))"
else
  say "Docker not found — installing…"
  case "$OS" in
    Linux)
      if have curl; then
        curl -fsSL https://get.docker.com | sudo sh
        sudo usermod -aG docker "$USER" 2>/dev/null || true
        warn "Added you to the 'docker' group — you may need to log out/in (or run: newgrp docker) for it to take effect."
      else
        die "curl is required to install Docker. Install curl, then re-run."
      fi ;;
    Darwin)
      if have brew; then brew install --cask docker && warn "Open Docker Desktop once to start the daemon, then re-run.";
      else die "Install Docker Desktop: https://www.docker.com/products/docker-desktop then re-run."; fi ;;
    *) die "Unsupported OS for auto-install. Install Docker manually, then re-run." ;;
  esac
fi

# daemon must be running
if ! docker info >/dev/null 2>&1; then
  case "$OS" in
    Linux) sudo systemctl start docker 2>/dev/null || sudo service docker start 2>/dev/null || true ;;
  esac
  docker info >/dev/null 2>&1 || die "Docker is installed but the daemon isn't running. Start Docker, then re-run."
fi
ok "Docker daemon running"

# ---- 2. Python 3 (+ venv) — the app runs on the host so it reaches the local cluster directly ----
if have python3 && python3 -c 'import venv' >/dev/null 2>&1; then
  ok "Python 3 present ($(python3 --version 2>&1))"
else
  say "Python 3 / venv not found — installing…"
  if   have apt-get; then sudo apt-get update -y && sudo apt-get install -y python3 python3-venv python3-pip curl tar
  elif have dnf;     then sudo dnf install -y python3 python3-pip curl tar
  elif have yum;     then sudo yum install -y python3 python3-pip curl tar
  elif have pacman;  then sudo pacman -Sy --noconfirm python curl tar
  elif have brew;    then brew install python
  else die "Couldn't auto-install Python 3. Install Python 3.12+, then re-run."; fi
fi

# ---- 3. hand off: run.sh installs kubectl/helm/kind/etc (only if missing), makes a cluster, serves ----
say "Environment ready — starting Helmsman via run.sh"
exec ./run.sh
