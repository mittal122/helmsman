# ⎈ Helmsman — AI Kubernetes Deployment Platform

An AI-powered "DevOps engineer as software": guides a non-expert developer through the
full deploy lifecycle — **containerize → configure → generate manifests → validate →
scan → deploy → verify → monitor → self-heal** — with **every command and error visible
in real time**, plus a full **SRE console** to manage any workload in any namespace.

> Deterministic core, thin LLM shell. Everything that touches the cluster is
> deterministic and tested; the LLM is advisory only and never writes the YAML applied
> to the cluster.

## Features

- **Deploy console** (`/`) — live activity stream: the real `helm`/`kubectl` commands,
  raw errors (with a copy-able report incl. the exact `file:line` when it's a platform
  bug), a 9-stage progress stepper, generated files, pod health, CPU/mem, and a
  **clickable auto-port-forward URL**.
- **SRE management console** (`/manage`) — browse **any namespace → any workload** (even
  ones Helmsman didn't deploy): full topology summary (**Service → Deployment → Pods** +
  HPA/PDB/Config), and actions: **scale, stop, restart, autoscale (HPA), logs, delete
  (2-step confirm), and ▶ Open app** (port-forward → clickable URL, auto-stops when you
  close the window).
- **Self-healing** — deterministic auto-rollback on failure (behind a circuit breaker),
  and when it can't auto-fix, clear guidance + an **AI fix-prompt you can paste into any
  AI**.
- **Validation gate** — `kubeconform` + `kubectl --dry-run=server` + `kube-score`, and a
  `trivy` image-scan gate, before anything reaches the cluster.
- **Security** — operator-token auth on every mutating endpoint, secret redaction
  (`••••`), RFC1123 input validation, subprocess timeouts, encrypted-at-rest kubeconfig
  store.
- **Durable** — Postgres event store + audit log (falls back to in-memory with no DB).

## Quick start (local)

```bash
./run.sh          # checks deps; auto-installs only what's missing into ./.bin; kind up; serve :8000
```
Open **http://localhost:8000**. Requires Docker/Podman + Python 3.12.

## Send it to someone (one portable file)

Build once, ship a single file — the receiver needs **only Docker** (no source, no
Python, no kubectl/helm/trivy, works offline):

```bash
./scripts/package.sh            # -> helmsman-image.tar.gz  (send this file)
```

Receiver:
```bash
docker load < helmsman-image.tar.gz
docker run -p 8000:8000 -e ALLOW_OPEN_DEV=1 -e COOKIE_INSECURE=1 \
           -v "$HOME/.kube:/home/appuser/.kube:ro" helmsman:1.0
# open http://localhost:8000  (mounts their kubeconfig so it can reach their cluster)
```
For durable history/audit, use the Compose stack below instead of `docker run`.

## Self-host (Docker + Postgres)

```bash
export AUTH_TOKEN=change-me
export KUBECONFIG_ENC_KEY=$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')
docker compose up --build
```
Mounts your `~/.kube/config` read-only so it can reach your cluster.

## Configuration (env vars)

| Var | Effect |
|---|---|
| `AUTH_TOKEN` | If set, every mutating endpoint requires `Authorization: Bearer <token>`. Unset = open (local dev). |
| `KUBECONFIG_ENC_KEY` | Fernet key enabling the encrypted kubeconfig store (multi-cluster). |
| `DATABASE_URL` | Postgres DSN for the durable event store + audit log. Unset = in-memory. |
| `KUBECONFIG` | Which cluster to manage (standard kube context). |
| `JWT_SECRET` | Signing secret for session tokens (set a strong value in prod; defaults to `AUTH_TOKEN`). |
| `BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` | Creates the first admin on empty DB. |
| `COOKIE_INSECURE=1` | Allow the session cookie over plain http (local dev only). Default: Secure (HTTPS-only). |
| `ALLOW_OPEN_DEV=1` | Zero-config open access when nothing is configured (local dev). **Unset in production** — auth is then always enforced (secure by default). |

## Endpoints (highlights)

`GET /healthz` · `GET /readyz` · `GET /history` · `GET /audit` · `POST /deploy` ·
`GET /namespaces` · `GET /namespaces/{ns}/workloads[/{name}]` · scale/stop/restart/
autoscale/logs/forward · `DELETE .../{name}?confirm=<name>`. Full OpenAPI at `/docs`.

## Development

```bash
cd backend && python -m pytest -q      # 134 tests
```

## Production maturity

Tier 1 (done): durable state (Postgres) + audit, health/readiness probes, containerized,
CI, MIT-licensed, operator-token auth, graceful shutdown.
Tier 2 (roadmap for multi-tenant SaaS): per-user RBAC + enforced kubeconfig isolation,
horizontal scale/HA, TLS + rate-limiting, cloud-cluster E2E, KEDA/VPA autoscaling.

## License

MIT — see [LICENSE](LICENSE).
