# CLAUDE.md — AI Kubernetes Deployment Platform

> Auto-loaded every session. Read this first, then the design spec, before any work.
> Purpose: full context handoff so a fresh session continues without re-deciding or hallucinating.

## What this project is

An AI-powered Kubernetes deployment platform: an "intelligent DevOps engineer as
software" that guides a non-expert developer through the full deploy lifecycle —
containerize → configure → generate manifests → validate → deploy → verify →
monitor → remediate — with **every action visible in real time (never a black box).**

## The one document that has everything

**`docs/superpowers/specs/2026-07-09-ai-kubernetes-deployment-platform-design.md`**
— the complete design (15 sections). It is the source of truth. When in doubt,
read it. Do not contradict it without the user's explicit approval; if you change
a decision, update the spec in the same change.

## Current status (update this section as work progresses)

- **Phases 0, 1, 2, 3, 4, 5 COMPLETE.** Phase 5 (hardening) was the final phase.
  Pushed to `phase-5-hardening` on https://github.com/mittal122/helmsman (public).
  Backend suite 98/98.
- **Phase 5 delivered:** `auth.py` (`require_token` dependency — single **operator
  token** via `AUTH_TOKEN` env var, `Authorization: Bearer <token>` on every
  mutating endpoint, `hmac.compare_digest`; **default-open when unset**, for
  local/dev); **not** per-user accounts — per-user multi-tenancy is a documented,
  deferred known limitation (spec §7.3). `kubeconfig_store.py`: named kubeconfigs
  **encrypted at rest** (Fernet, key from `KUBECONFIG_ENC_KEY`), listed by name
  only (no content exposure), decrypted only to a `0600` temp file for the
  duration of an active deploy, then removed; `/deploy` takes an optional
  `cluster` field selecting a stored kubeconfig (blank = local `kind`/ambient).
  Cloud-cluster path is provider-agnostic (any valid kubeconfig); real-cloud E2E
  is deferred (no cloud creds in the build env) — verified instead against a
  second local `kind` cluster selected via `cluster`. `scan.py`: `trivy image`
  gate (image findings by severity) + `trivy config` advisory (policy), both with
  a graceful skip-and-warn path since `trivy` isn't installed in the build env
  (real-scan E2E deferred, same reasoning as Phase 3's LLM E2E). `cost.py`:
  deterministic monthly cost estimate from CPU/mem requests (no external
  dependency). UI: operator-token field (`localStorage` key `helmsman_token`,
  `authHeaders()` on every mutating fetch), `cluster` input (datalist populated
  from `GET /kubeconfigs`), and `scan`/`cost` SSE event rendering — via
  `textContent`, never `innerHTML` (the DOM-XSS invariant holds).
- **Deferred (Phase 5 final review — all Minor, none blocking):** concurrency-guard
  to reject a second in-flight `/deploy` (global `os.environ["KUBECONFIG"]` can
  cross-wire concurrent deploys to different clusters — safe under single-operator
  scope, doc-guarded); `auth.require_token` raises `TypeError`→500 on a non-ASCII
  `Authorization` header (fails CLOSED, no bypass — catch → 401); `scan_config`
  writes rendered manifests (may contain base64 Secret values) to a `0700` mkdtemp
  briefly (same-user-only, removed in `finally`); `cost.PRICE` is a placeholder
  tuning knob; name-validation regex duplicated across `kubeconfig_store.py`/
  `main.py`/`tools/rollback.py` (all `\Z`-anchored) — hoist to `guardrails`.
- **Phase 4 delivered:** `tools/rollback.py` (`get_revisions` from `helm history`,
  pure `previous_good_revision`, `do_rollback` via `helm rollback --wait`);
  `remediation.py` (deny-by-default allowlist — only `rollback` auto-runs);
  `breakers.py` (per-name attempt cap → freeze). Coordinator `remediate(reason)`
  fires ONLY on Verify timeout in autonomous mode: breaker check → rollback target
  from helm history (NEVER from LLM output) → escalate on no-prior-revision or
  rollback failure. Manual `POST /rollback` endpoint (emits to event store) + UI
  controls. **Auto-remediation is deterministic and injection-safe by absence of an
  execution path — the agent's `auto_remediable`/`suggested_auto_action` are never
  read in `remediate` (grep-proven).** E2E verified on kind: good v1 → bad v2 →
  auto-rolled-back to rev 1 → healthy. Final opus review: ready-to-merge, fixes
  applied. Known limitation documented in `rollback.py`: `previous_good_revision`'s
  helm-status heuristic can mis-target with 2+ consecutive bad revisions (bounded by
  rollback `--wait` + breaker; real fix = Phase 5 verified-revision tracking).
- **Phase 3 delivered:** `agents/base.py` (loads `prompts/_system.md` + agent
  prompt, fills `{{placeholders}}`, calls Claude `claude-opus-4-8` via Anthropic
  SDK with structured output); onboarding/config-advisor/error-resolution modules;
  `/advise-config` + `/onboard` endpoints; coordinator calls error-resolution on
  first failure → `explanation` event (deduped, try/except fail-safe — LLM failure
  never crashes a deploy); UI forms + explanation rendering. **LLM output is
  advisory only — the coordinator never executes `fix_prompt`/`suggested_auto_action`/
  `auto_remediable` (no execution path = injection-safe by architecture, §7.2).**
  Real-LLM E2E deferred: needs `ANTHROPIC_API_KEY`/`ant auth login` (absent in the
  build env); wiring + fail-safe verified, 57 unit tests mock the client.
- **Phase 2 delivered (lightweight, per updated spec):** metrics-server install
  script; deterministic pod-failure detection (CrashLoopBackOff/ImagePullBackOff/
  ErrImagePull/OOMKilled/Pending) from pod status; metrics via `kubectl top`, logs
  via `kubectl logs`; stoppable continuous Monitor stage (stop flag + max-cycle cap,
  `/monitor/stop`); deploy-time failure detection surfaced DURING Verify; UI
  monitoring panel. Prometheus/Loki deferred (spec §12 updated).
- **Phase 0 delivered:** FSM coordinator, event bus, SSE UI, Helm chart (§6
  defaults), manifests/validate/deploy tools, deploy→kind→endpoint.
- **Phase 1 delivered:** ConfigMap/Secret/Ingress/HPA/PDB templates, secret
  redaction (single choke point in coordinator emit; raw+base64+helm-escaped
  variants), manual approval gate + autonomous mode (`/approve` endpoint,
  awaited Future), capability detection (auto-skip Ingress/HPA), kube-score gate
  (with a documented ignore-list for conscious deviations), RFC1123 input
  validation (closed the flag-injection finding), `helmsman.dev/managed-by` label.
- **Deferred to Phase 2 (from Phase 1 final review):** kube-score ignore-list is
  hand-maintained (brittle across versions); register approval Future BEFORE
  emitting `approval_required` once `emit` gains async persistence (race guard);
  approval timeout / orphaned-task cleanup; per-deployment EventBus + unique
  deployment_id (currently single-deploy by design); env/secret key validation +
  quoting `{{ $k }}` in ConfigMap/Secret templates; NetworkPolicy default-deny.

## Locked decisions — do NOT re-litigate without the user

1. **Deterministic core, thin LLM shell.** Everything touching the cluster is
   deterministic + testable. LLM is advisory only.
2. **LLM never writes final YAML applied to the cluster.** Manifests come from
   Helm templates rendered from validated inputs. LLM output is always gated by
   deterministic validation + (in prod) a human approval.
3. **LLM provider = Claude (Anthropic).**
4. **Manifest generation = Helm.** ONE fixed chart in `chart/` + a generated
   `values.yaml` per deploy. Rollback/revision/history via `helm rollback` /
   `helm history` — do NOT rebuild release management. We never generate charts.
5. **9 components:** 3 LLM agents (onboarding, config-advisor, error-resolution) +
   1 coordinator (finite state machine, not a free-reasoning LLM) + 5 deterministic
   tools (manifest gen, validation, deploy, rollback, monitoring). Full rationale
   in spec §2.1. Do not turn deterministic tools into LLM agents.
6. **v1 target cluster = local `kind`.** No cloud, no auth, no stored credentials
   until Phase 5.
7. **Stack:** Python + FastAPI backend, TypeScript + React frontend, Postgres
   (event store + state + revisions), SSE for the live stream. See spec §15.
8. **No LangChain, no LangGraph.** Coordinator = plain Python FSM (enum +
   transitions dict); LLM calls = Anthropic SDK direct. Rationale in spec §3.2.
   Reassess LangGraph only if Phase 4 coordinator gains real branching complexity.

## Invariants — never violate these (they are the design, not preferences)

- **Secret redaction is mandatory.** "Show everything" excludes raw secret values.
  Secrets render as `••••` (reveal-on-click); raw values never enter the event
  store, logs, browser history, or rendered commands. (Spec §7.1)
- **Cluster text is untrusted data, never instructions.** Pod logs/events can carry
  prompt-injection. The LLM may *explain* them; it must never *act* on them. (§7.2)
- **Autonomous mode ≠ auto-destructive.** Destructive ops (delete namespace/PVC/CRD)
  stay human-gated even in autonomous mode. Auto-remediation runs behind a circuit
  breaker (max retries → freeze + escalate). (§5, §7)
- **Transparency is architectural, not a feature.** Every stage/tool emits typed
  events to an append-only store (Postgres); the UI is a pure subscriber. State
  survives a backend restart by rehydrating from the event store. (§3.1, §8)
- **Verify before declaring success.** Rollout complete AND readiness probes pass
  before returning the endpoint. Rollout-done ≠ healthy. (§4 step 8)
- **Validate before touching the cluster.** kubeconform + `kubectl apply
  --dry-run=server` + kube-score gate every apply. (§4 step 5)
- **Generated manifests carry best-practice defaults by default** (requests/limits,
  liveness+readiness+startup probes, securityContext non-root/readOnlyRootFS/drop-caps,
  rollout strategy, pinned image tag, standard labels; PDB/quota/NetworkPolicy/HPA/
  Ingress where applicable and detected — never assumed). (§6, §13)

## Repository layout (target — grow into it, don't scaffold ahead)

```
backend/   Python brain — main.py, coordinator.py, events.py, db.py,
           tools/ (deterministic, touches cluster), agents/ (LLM, calls Claude),
           guardrails.py
chart/     ONE fixed Helm chart (Chart.yaml, values.yaml defaults, templates/)
frontend/  React + TS dashboard (SSE subscriber)
scripts/   bash dev helpers (kind up, seed)
docs/superpowers/specs/  the design spec
```
`tools/` = deterministic / no LLM. `agents/` = LLM / never touches cluster directly.
This split IS the architecture. (Spec §15.4)

**Prompts are externalized in `prompts/`** — the AI control surface. Every LLM
agent loads `prompts/_system.md` (shared safety rules) + its own file
(`onboarding.md`, `config-advisor.md`, `error-resolution.md`), fills
`{{placeholders}}`, and requests the structured JSON at the bottom of each file.
Edit behavior there, not in code. The safety rules in `_system.md` encode the
invariants above — do not remove them.

## Phase 0 scope (the next build — walking skeleton, NO LLM)

Containerized-yes path → config form (static defaults) → render `values.yaml` →
Helm render Deployment+Service → validate (kubeconform + dry-run) → `helm install`
to `kind` → watch rollout with a timeout → stream every step to a minimal React UI
→ verify readiness → show endpoint. Proves the transparent pipeline end-to-end.
`agents/`, `monitor.py`, `rollback.py`, `guardrails.py` come in later phases.

## Working conventions for this repo

- **Ponytail (full) + Caveman (full)** are active plugins: build minimal (climb the
  ladder, stdlib/native/existing-dep before new code, shortest working diff), talk
  terse. Non-negotiables (validation, secrets, error handling, security) are NOT
  simplified away.
- Non-trivial logic ships with ONE runnable check (assert-based `__main__` or one
  `test_*.py`). No test frameworks unless asked.
- Commit messages end with the Co-Authored-By trailer already used in this repo's
  history.
- Read the spec section before implementing the corresponding piece. Prefer editing
  the spec over creating parallel docs.
