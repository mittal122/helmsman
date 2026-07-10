# ⎈ Helmsman — AI Kubernetes Deployment Platform

An AI-powered "DevOps engineer as software": guides a non-expert developer through the
full deploy lifecycle — **containerize → configure → generate manifests → validate →
scan → deploy → verify → monitor → self-heal** — with **every command and error visible
in real time**, plus a full **SRE console** to manage any workload in any namespace.

> Deterministic core, thin LLM shell. Everything that touches the cluster is
> deterministic and tested; the LLM is advisory only and never writes the YAML applied
> to the cluster.

## 🚀 Get started (clone & run — that's it)

You do **not** need Docker, Kubernetes, or any tool installed first. The setup script
checks your machine and installs **only what's missing**, creates a local cluster, and
starts the app.

**Linux / macOS / WSL2:**
```bash
git clone https://github.com/mittal122/helmsman
cd helmsman
./setup.sh
```

**Windows 10/11:**
```bat
git clone https://github.com/mittal122/helmsman
cd helmsman
setup.bat
```
> No git? Download the ZIP from the GitHub page (green **Code** button → **Download ZIP**),
> extract it, then run the script inside.

Then open **http://localhost:8000**.

First run takes a few minutes (it downloads what's missing) and needs an internet
connection. Everything after that is instant. On Linux it may ask for your password
(to install Docker) — if it just installed Docker, log out/in once and re-run `./setup.sh`.

## Deploy your app

1. Open **http://localhost:8000**.
2. In **Deploy**, either enter a **container image** (e.g. `yourname/your-app:1.0`) **or a
   Git repo URL** to build from source — plus a name, port, and replicas → hit **Deploy**.
   Watch every step live.
3. Use **Manage** to scale, view logs, autoscale, open the app's URL, or delete — for any
   app in any namespace.

> **Deploy from source:** put your repo's URL in the **Git repo** field (leave Image blank).
> Helmsman clones it, builds its `Dockerfile`, loads the image straight into your local
> cluster (kind/minikube — no registry needed), and deploys it. For a remote cluster, set
> `REGISTRY` to a registry it can pull from. Needs Docker on the machine running Helmsman.
> No Dockerfile yet? Use **"Ask agent: containerize"** to generate one from a description.

## Configuration (optional)

All via environment variables — the defaults work for local use:

| Var | Effect |
|---|---|
| `AUTH_TOKEN` / users | Turn on login + roles (viewer/operator/admin). Unset = no login (local). |
| `DATABASE_URL` | Postgres for durable deploy history + audit log. Unset = in-memory. |
| `KUBECONFIG` | Which cluster to manage. The setup script points at the local one it created. |
| `KUBECONFIG_ENC_KEY` | Enables the encrypted multi-cluster kubeconfig store. |
| `REGISTRY` | Push target for **deploy-from-Git** builds when the cluster is remote (kind/minikube need none). |

## Features

- **Deploy from image or Git** — deploy a pre-built image, or point at a **Git repo** and
  Helmsman clones it, builds the `Dockerfile`, and loads the image into your local cluster
  (no registry needed) — then deploys through the same validated pipeline.
- **Deploy console** — live stream of the real `git`/`docker`/`helm`/`kubectl` commands +
  raw errors (with a copy-able report incl. the exact `file:line`), a staged progress bar,
  generated files, pod health, CPU/mem, and a **clickable auto-port-forward URL**.
- **SRE management console** — browse any namespace → any workload: topology summary
  (**Service → Deployment → Pods** + HPA/PDB/Config) + scale/stop/restart/autoscale/logs/
  delete (2-step)/**▶ Open app**.
- **Self-healing** — deterministic auto-rollback on failure, and when it can't auto-fix,
  clear guidance + an **AI fix-prompt you can paste into any AI**.
- **Validation + scan gate** — `kubeconform` + `kubectl --dry-run=server` + `kube-score` +
  `trivy` before anything reaches the cluster.
- **Multi-user (optional)** — accounts, JWT sessions, RBAC, per-user audit trail.
- **Durable** — Postgres event store + audit log (in-memory fallback with no DB).

## Development

```bash
cd backend && python -m pytest -q      # 152 tests
```

## Advanced — other ways to run (only if you already know Docker)

<details><summary>Already have Docker + a cluster · self-host with Postgres · send a single file</summary>

```bash
# Already have Docker + a running cluster? Run the published image directly:
docker run --rm --network host -e ALLOW_OPEN_DEV=1 -e COOKIE_INSECURE=1 \
  -v "$HOME/.kube/config:/home/appuser/.kube/config:ro" mittal122/helmsman:1.0

# Self-host with durable Postgres (multi-user, audit):
export AUTH_TOKEN=change-me
docker compose up          # app + Postgres

# Package into ONE file to send to someone (they only need Docker):
./scripts/package.sh       # -> helmsman-image.tar.gz
#   receiver: docker load < helmsman-image.tar.gz  &&  docker run ... helmsman:1.0
#   Apple Silicon / ARM:  ./scripts/package.sh helmsman:1.0 linux/arm64
```
Image on Docker Hub: `mittal122/helmsman:1.0`.
</details>

## License

MIT — see [LICENSE](LICENSE).
