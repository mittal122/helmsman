#!/usr/bin/env bash
set -euo pipefail
# Adopt-don't-build: official installer. Pins to a recent release line.
curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh \
  | sh -s -- -b /usr/local/bin v0.58.0
trivy --version
