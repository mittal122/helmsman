#!/usr/bin/env bash
set -euo pipefail
kind get clusters | grep -qx helmsman || kind create cluster --name helmsman
kubectl cluster-info --context kind-helmsman
