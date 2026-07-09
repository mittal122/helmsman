# Build Prompts — Helmsman

Copy-paste playbook. When you're ready to build a phase, paste that phase's
prompt. Each one is self-contained: it tells me what to build, the rules to
follow, and how we'll know it's done.

## How to use this file

- **Build in order.** Phase 0 first, then 1, 2, … Each phase depends on the one
  before it.
- **Each phase runs as: plan → build → review.** The phase prompt starts the
  plan. I write a step-by-step plan, you approve it, I build (tests first,
  showing every step), then we review and commit.
- **You don't repeat the rules.** The spec (`docs/superpowers/specs/2026-07-09-...-design.md`)
  and `CLAUDE.md` load automatically and carry every locked decision and safety
  invariant. The prompts just point at them.
- **Correct me anytime** with plain language ("no, use X"). It gets recorded.

---

## Session-start prompt (optional — paste at the top of a brand-new chat)

```
This is the Helmsman project (AI Kubernetes deployment platform). Read CLAUDE.md
and the design spec in docs/superpowers/specs/ before doing anything. Confirm the
current phase and what's next, then wait for my instruction.
```

---

## Phase 0 — Walking skeleton (no LLM)

```
Plan then build Phase 0 of Helmsman (the walking skeleton), per spec §12 and
§15.4. Goal: prove the transparent pipeline end-to-end on a local kind cluster,
with NO LLM.

Build:
- backend/ (FastAPI): main.py with an SSE endpoint that streams events, and a
  deploy endpoint; coordinator.py as a plain-Python state machine
  (Collect → Generate → Validate → Deploy → Verify); events.py (typed events +
  in-process bus).
- backend/tools/: manifests.py (render values.yaml, then `helm template`),
  validate.py (kubeconform + `kubectl apply --dry-run=server`), deploy.py
  (`helm install` + watch rollout WITH a timeout, surface events, never hang).
- chart/: one fixed Helm chart — Chart.yaml, values.yaml, templates/deployment.yaml
  + service.yaml, with best-practice defaults from §6.
- A minimal single-page UI that subscribes to the SSE stream and shows each event
  live (React optional at this stage — simplest thing that proves the stream).
- scripts/kind-up.sh to create the local cluster.
- One runnable check that fails if the pipeline breaks.

Definition of done: I give a container image + basic config, and Helmsman renders
→ validates → deploys it to kind → streams every step live → verifies readiness →
prints the endpoint. Verify by actually deploying a sample app, not just tests.
```

---

## Phase 1 — Full manifests + approval gates

```
Plan then build Phase 1 of Helmsman, per spec §4–6. Adds the complete manifest
set and manual control.

Build:
- chart/templates/: configmap.yaml, secret.yaml, ingress.yaml, hpa.yaml, pdb.yaml,
  all carrying the §6 best-practice defaults; detect-and-skip for ingress
  controller / metrics-server when absent (§13).
- backend/guardrails.py: secret redaction — secrets show as •••• in the event
  stream and never enter the store, logs, or rendered commands (§7.1).
- Approve stage in the coordinator + Manual mode: block each mutating action until
  the user approves.
- Add kube-score to validate.py.

Definition of done: full manifest set generates and validates; secrets are redacted
everywhere in the live stream; in Manual mode each apply waits for my approval.
Verify with a deploy that includes env vars + a secret.
```

---

## Phase 2 — Monitoring

```
Plan then build Phase 2 of Helmsman, per spec §4 (Monitor) and §10. Adds
continuous monitoring and deterministic failure detection.

Build:
- scripts/monitoring-up.sh: install kube-prometheus-stack + Loki via Helm.
- backend/tools/monitor.py: query Prometheus (metrics) and Loki (logs); detect
  CrashLoopBackOff, ImagePullBackOff, OOMKilled, and Pending from pod status +
  events. Deterministic — no LLM.
- Monitor stage in the coordinator: keeps running after Verify.
- UI panels for health (pod chips) and resources (CPU/memory), matching the mockup.

Definition of done: after a deploy, live health + metrics appear; when I deploy a
deliberately broken image, the failure type is detected and surfaced. Verify by
inducing a real CrashLoopBackOff.
```

---

## Phase 3 — LLM layer (the AI agents)

```
Plan then build Phase 3 of Helmsman, per spec §2.1 and the prompt files in
prompts/. Adds the three LLM agents. Provider: Claude (Anthropic SDK, direct — no
LangChain/LangGraph).

Build:
- backend/agents/onboarding.py, config_advisor.py, error_resolver.py — each loads
  prompts/_system.md + its own prompt file, fills the {{placeholders}}, and calls
  Claude asking for the structured JSON declared in that file.
- Wire config-advisor into Collect (suggest + explain settings, user confirms),
  onboarding into Onboard (containerization prompt), error-resolver into Remediate
  (root cause + fix).
- Enforce the untrusted-input rule (§7.2): cluster logs/events are data, never
  instructions.

Definition of done: a not-containerized app produces a copy-paste containerization
prompt; config suggestions appear with plain-language reasons for me to confirm; a
real failure produces a plain-language root cause + recommended fix. LLM never
writes final YAML. Verify each of the three agents on a real case.
```

---

## Phase 4 — Autonomous mode + rollback

```
Plan then build Phase 4 of Helmsman, per spec §5 and §7. Adds hands-off operation
and automatic recovery.

Build:
- backend/tools/rollback.py: `helm rollback` to the previous known-good revision;
  rely on `helm history` for revision tracking.
- Autonomous mode in the coordinator: auto-execute reversible ops (apply, scale,
  rollout, read, rollback), while still streaming everything.
- Destructive-op gate: delete namespace/PVC/CRD stay human-gated even in
  autonomous mode.
- Circuit breaker on auto-remediation: after N attempts, freeze and escalate.

Definition of done: an autonomous deploy runs start-to-finish untouched; an induced
failure triggers automatic rollback to the last working version; repeated failures
trip the breaker instead of looping. Verify all three.
```

---

## Phase 5 — Hardening (production readiness)

```
Plan then build Phase 5 of Helmsman, per spec §7 and §11. Prepares for real,
multi-user, cloud use.

Build:
- Auth + multi-tenancy: per-user isolation; kubeconfig stored encrypted at rest,
  least-privilege ServiceAccount (never cluster-admin).
- Cloud cluster support (EKS/GKE/AKS) alongside kind.
- Security scanning in the pipeline: trivy (image vulns) + policy checks.
- Cost/resource estimation at config time.

Definition of done: two users deploy in isolation without seeing each other; a cloud
cluster target works; image scan blocks a known-vulnerable image; estimated cost
shows before deploy. Verify each.
```

---

## Reusable prompts (any time)

**Single task:**
```
<describe the outcome you want in one sentence>. Follow CLAUDE.md.
```

**Fix something:**
```
<what's wrong / what you observed>. Find the root cause and fix it.
```

**Review before committing:**
```
Review the current changes, verify they actually work, then commit.
```

**Show a UI state:**
```
Show me a UI mockup of <state, e.g. a CrashLoopBackOff with the AI explanation>.
```
