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

- **Phase: DESIGN COMPLETE. No code written yet.** Next deliverable = Phase 0
  implementation plan (via the `writing-plans` skill), then Phase 0 code.
- Spec written + committed (4 commits, latest adds §15 Tech foundation).
- **One open question (§14):** is Phase 2 monitoring in the first implementation
  plan, or is Phase 0 built alone first? Recommendation on record: **Phase 0 alone.**

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
