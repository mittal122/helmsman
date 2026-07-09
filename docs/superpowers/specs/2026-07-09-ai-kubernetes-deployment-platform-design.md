# AI-Powered Kubernetes Deployment Platform — Design

**Date:** 2026-07-09
**Status:** Draft for review
**v1 target cluster:** local `kind` / `minikube`
**Scope of this doc:** full architecture + phased roadmap. No code produced this round.

---

## 1. Product vision

An intelligent DevOps engineer, as software. It guides a developer — who may not
know Docker or Kubernetes — through the entire deployment lifecycle: containerize
→ configure → generate manifests → validate → deploy → verify → monitor →
remediate. Every action is visible in real time. It is never a black box.

Target user: a developer with a working app but shallow infra knowledge. The
platform automates everything safe to automate, while an advanced user can drive
every step manually.

**The moat is not "generates YAML."** It is: guided onboarding + a fully
transparent narrated pipeline + LLM root-cause explanation of failures. Manifest
generation, validation, deployment, and monitoring are solved problems we wire
up from proven tools.

---

## 2. Core architectural principle

> **Deterministic core, thin LLM shell.**

Everything that touches the cluster is deterministic, predictable, and testable.
The LLM layer is advisory and explanatory. **LLM output is never applied to the
cluster without a deterministic validation gate and (in production) a human
gate.** LLM-generated YAML applied to a live cluster is the single largest
failure mode this design exists to avoid.

### 2.1 Why most "agents" are not agents

The original vision listed ~7 AI agents. Most involve zero reasoning and are
deterministic pipelines. Making them LLM agents adds cost, latency, and
non-determinism for no benefit.

| Component | Implementation | LLM? |
|---|---|---|
| Coordinator | State machine (FSM) over the deployment lifecycle | Plans *within* a stage only |
| Manifest generation | Template render (Helm/Jinja) from validated inputs | **No** — hallucinated YAML = outage |
| Validation | `kubeconform` + `kubectl apply --dry-run=server` + `kube-score` | **No** |
| Deployment | Kubernetes client `apply` + watch rollout | **No** |
| Rollback | `kubectl rollout undo` / revision restore | **No** — one command |
| Health monitoring (SRE) | Prometheus alert rules + Loki queries | **No** — detection is deterministic |
| Error resolution | Read failure events/logs → explain → generate fix-prompt | **Yes** ✅ |
| Onboarding | Generate containerization prompt for the user's own AI assistant | **Yes** ✅ |

**LLM is used in exactly three places:** (a) containerization-prompt generation,
(b) failure root-cause explanation + fix-prompt generation, (c) coordinator
planning within a stage. Everything else is deterministic tooling.

---

## 3. System architecture

```
┌──────────────────────────────────────────────────────┐
│  Dashboard UI   ← subscribes to event stream (SSE)     │
└───────────────────────▲──────────────────────────────┘
                        │ typed events
┌───────────────────────┴──────────────────────────────┐
│  Event Bus  +  Event Store (Postgres, append-only)     │
│  every tool/stage emits typed events; UI is a subscriber│
└───────────────────────▲──────────────────────────────┘
                        │
┌───────────────────────┴──────────────────────────────┐
│  COORDINATOR  =  finite state machine                  │
│  Onboard → Collect → Generate → Validate → Approve      │
│         → Deploy → Verify → Monitor → Remediate         │
└──┬─────────────┬─────────────┬────────────┬───────────┘
   │             │             │            │
 DETERMINISTIC TOOLS           │      LLM SERVICES (thin, advisory)
  • manifest render (Helm/Jinja)│       • containerize-prompt gen
  • validate (kubeconform +     │       • root-cause explain
      dry-run + kube-score)     │       • fix-prompt gen
  • deploy (k8s client)         │
  • rollback (revision restore) │
  • monitor (Prometheus/Loki API)
        │
   GUARDRAIL LAYER (wraps every cluster mutation)
     • secret redaction   • RBAC-scoped ServiceAccount
     • destructive-op gate • circuit breaker on auto-remediation
```

### 3.1 The event bus is the backbone of transparency

Transparency is not a UI feature bolted on later — it is the architecture. Every
stage and tool emits **typed events** (`ManifestGenerated`, `CommandExecuted`,
`ValidationResult`, `RolloutProgress`, `AlertFired`, `LLMDecision`, …) to an
append-only store. The UI is purely a subscriber. Consequences:

- "Show everything" is automatic — the UI renders the event log.
- State survives a backend restart (events are persisted).
- Postmortems are free — the event log *is* the audit trail.

### 3.2 Coordinator is a state machine, not an improvising LLM

The deployment lifecycle is a known DAG. The coordinator is a finite state
machine that advances through stages. The LLM may *plan within* a stage
(e.g. which fix to suggest) but never invents the flow. This makes the system
predictable and testable.

---

## 4. Deployment lifecycle (stages)

1. **Onboard** — "Is your app containerized?" If no → LLM generates a
   containerization prompt the user gives to their own AI assistant (Claude,
   ChatGPT, etc.) to produce a Dockerfile. Platform does not build the image for
   them in v1; it verifies an image reference exists before proceeding.
2. **Collect (app)** — name, image, namespace, port, replicas, deployment mode.
3. **Collect (config)** — env vars, `.env` values, secrets, key-value config.
   **Secrets are redacted in the event stream from the moment they are entered.**
4. **Generate** — render all manifests from templates with best-practice defaults
   (§6). Every rendered file is emitted as an event and shown in the UI.
5. **Validate** — `kubeconform` (schema) + `kubectl apply --dry-run=server`
   (cluster-side admission) + `kube-score` (best-practice lint). Blocks on error.
6. **Approve** — manual mode: user approves each mutating action. Autonomous
   mode: auto-proceed for reversible ops; destructive ops still gated (§7).
7. **Deploy** — apply manifests via the Kubernetes client, watch rollout.
8. **Verify** — rollout complete AND readiness probes pass before declaring
   success. Return endpoint, service info, ports, ingress URL, replica status.
9. **Monitor** — continuous, via Prometheus + Loki. Deterministic detection of
   CrashLoopBackOff, ImagePullBackOff, OOMKilled, Pending, unavailability.
10. **Remediate** — on a fired alert: LLM explains root cause in plain language,
    generates a fix-prompt, recommends a solution. Auto-recover / rollback where
    safe, behind a circuit breaker.

---

## 5. Deployment modes (corrected definition)

- **Manual mode** — user approves every mutating action before execution.
- **Autonomous mode** — auto-executes **reversible, non-destructive** ops
  (apply, scale, rollout, read, rollback-to-known-good) while streaming every
  action. **Destructive ops (delete namespace/PVC/CRD) remain human-gated even
  in autonomous mode.** Auto-remediation runs behind a circuit breaker (max
  retries, then freeze + escalate) to prevent flapping.

---

## 6. Generated-manifest best-practice defaults (non-negotiable)

Every generated Deployment carries, by default:

- resource `requests` and `limits`
- `liveness`, `readiness`, and `startup` probes
- `securityContext`: `runAsNonRoot`, `readOnlyRootFilesystem`, drop ALL caps
- rollout strategy (`maxUnavailable` / `maxSurge`)
- pinned image tag/digest — never `:latest`
- standard labels (`app.kubernetes.io/*`)

Alongside, where applicable: `PodDisruptionBudget`, namespace `ResourceQuota` +
`LimitRange`, default-deny `NetworkPolicy`, `imagePullSecrets`, `HPA` (requires
metrics-server — **detected**, not assumed), `Ingress` (requires an ingress
controller + DNS/TLS — **detected**, not assumed).

---

## 7. Security (the part that kills platforms like this)

1. **Secret redaction is mandatory.** "Show everything" conflicts with secrets.
   Secrets appear as `••••` with reveal-on-click; raw values never enter the
   event store, logs, or browser history.
2. **Prompt injection from cluster text.** Pod logs and events are untrusted
   input. An attacker or noisy dependency can embed instructions in a log line.
   The LLM may *explain* cluster text; it may **never gate an action on it**.
   All cluster-sourced text is data, never instructions.
3. **Kubeconfig / credentials are crown jewels.** v1 targets local `kind`, which
   sidesteps stored-cloud-credential risk. When cloud clusters arrive (Phase 5):
   encryption at rest, short-lived scoped tokens, least-privilege ServiceAccount
   (never cluster-admin), per-user isolation.
4. **Destructive-op gate** — see §5.
5. **Cost control** — Prometheus detects (deterministic, free); the LLM is
   invoked only on a fired alert. Metrics are never streamed into an LLM.

---

## 8. Persistence & real-time transport

- **Postgres** — event store + deployment state + approvals + revision history.
  In-memory state loses the deployment on a crash; not acceptable.
- **Revision history** — record each successful deploy as "known good" so
  rollback has a target. Rollback is meaningless without it.
- **SSE (server-sent events)** — one-directional live stream to the UI. WebSocket
  only if bidirectional need appears (YAGNI until then).

---

## 9. Dashboard UI

A professional deployment dashboard (not a basic dashboard) rendering the event
stream: current stage, currently executing tool/stage, generated manifests,
generated code, executed commands, deployment logs, monitoring metrics, health,
status, rollback progress, final summary. Experience = watching an experienced
DevOps engineer work. Because the UI is a pure event-stream subscriber, "show
everything" is structural, not per-feature work.

---

## 10. Tech stack (v1)

| Concern | Choice | Rationale |
|---|---|---|
| Backend | FastAPI (Python) | Best Kubernetes client + LLM ecosystem |
| Cluster access | official `kubernetes` Python client | Standard, typed, watch support |
| Manifest gen | Helm or Jinja templates | Never LLM; templates render validated inputs |
| Validation | `kubeconform` + `kubectl --dry-run=server` + `kube-score` | Deterministic, layered |
| Monitoring | kube-prometheus-stack + Loki (helm) | Adopt, don't build |
| State | Postgres | Durable event store + revisions |
| Live stream | SSE | Simplest transport that fits |
| Dev cluster | `kind` | No cloud cost, no credential risk |
| UI | React | Standard dashboard stack |

**Adopt, don't build:** image scanning (`trivy`), policy (`kube-score` /
Kyverno), GitOps/drift (Argo CD / Flux) — pulled in at later phases, never
reinvented.

---

## 11. Non-goals / out of scope for v1

- Cloud clusters, cloud auth, stored cloud credentials (Phase 5).
- Building the container image for the user (v1 only verifies an image ref).
- Multi-tenancy / user auth (Phase 5).
- Canary / blue-green progressive rollout (later).
- GitOps reconciliation (adopt Argo/Flux later if needed, do not build).

---

## 12. Phased roadmap

Each phase ships something runnable. Local `kind` throughout v1.

- **Phase 0 — Walking skeleton (no LLM).**
  Containerized-yes path → config form → render Deployment+Service from template
  → validate (kubeconform + dry-run) → apply to `kind` → watch rollout → stream
  every step to a basic UI → show endpoint. *Proves the transparent pipeline
  end-to-end. This is the spine every later phase hangs on.*

- **Phase 1 — Full manifests + approval gates.**
  ConfigMap / Secret (redacted) / Ingress / HPA / PDB + best-practice defaults
  (§6) + manual approval mode + `kube-score`.

- **Phase 2 — Monitoring.**
  Deploy kube-prometheus-stack + Loki. Deterministic failure detection
  (pod phase + events: CrashLoopBackOff, ImagePullBackOff, OOMKilled, Pending).
  Live metrics + logs in the UI.

- **Phase 3 — LLM layer (thin).**
  Containerization-prompt generator; failure root-cause explanation + fix-prompt
  from real events. Prompt-injection guardrails (§7.2).

- **Phase 4 — Autonomous mode + rollback.**
  Revision tracking, auto-remediation with circuit breaker, destructive-op gate.

- **Phase 5 — Hardening.**
  Auth, multi-tenant kubeconfig isolation + encryption, cloud clusters, image /
  policy scanning, cost estimation.

---

## 13. Key edge cases to design against

- Ingress requested but no ingress controller installed → detect, warn, degrade
  to NodePort/port-forward.
- HPA requested but no metrics-server → detect, warn, skip HPA.
- Rollout stuck (never becomes Ready) → timeout + surface events, do not hang.
- Namespace missing → create or prompt, per mode.
- Image ref invalid / unreachable registry → fail at Verify, not silently.
- Secret entered then displayed in a copied command → redaction must cover
  rendered commands, not just form fields.
- Auto-remediation oscillation → circuit breaker freezes after N attempts.
- Backend restart mid-deploy → state rehydrates from the event store.

---

## 14. Open questions for the next round

1. LLM provider for Phase 3 (Claude default, given this platform).
2. Helm vs. raw Jinja templates for manifest generation (leaning Helm — it also
   gives us rollback/revision semantics for free).
3. Whether Phase 2 monitoring is in-scope for the first implementation plan or
   deferred behind Phase 0–1.
