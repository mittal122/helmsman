# Phase 4 — Autonomous Mode + Rollback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add automatic recovery. In autonomous mode, when a deploy fails and a prior good revision exists, the coordinator rolls back to it — a deterministic, always-safe, reversible action — behind a circuit breaker (max attempts → freeze + escalate) and a deny-by-default destructive-op gate. A manual `/rollback` endpoint lets the user trigger rollback in manual mode.

**Architecture:** Auto-remediation is **deterministic, not LLM-driven** — the error-resolution agent's `auto_remediable`/`suggested_auto_action` remain advisory (emitted only); the coordinator decides to roll back from `helm history` (a prior good revision), never by executing an LLM string. A new `Remediate` coordinator step runs after a Verify timeout in autonomous mode: check the circuit breaker, find a prior good revision, confirm the action is on the safe allowlist, then `helm rollback`. First-deploy failures (no prior revision) escalate to a human. Deletes and any non-allowlisted action never auto-run.

**Tech Stack:** Python 3.12, pytest; Helm, kubectl, kind.

## Global Constraints

- **Autonomous ≠ auto-destructive.** Only `rollback` (safe, reversible) auto-runs. Destructive ops (delete namespace/PVC/CRD) stay human-gated even in autonomous mode — enforced by a deny-by-default allowlist. (Spec §5, §7)
- **Auto-remediation runs behind a circuit breaker** — max retries per deployment, then freeze + escalate. (Spec §5)
- **Deterministic core, LLM advisory.** The rollback decision comes from `helm history`, never from the LLM's `auto_remediable`/`suggested_auto_action` (those stay emitted-only). No code path executes agent output. (CLAUDE.md #1, §7.2)
- **Rollback/revisions via Helm** — `helm rollback` / `helm history`; do not rebuild release management. (CLAUDE.md #4)
- **Never hang** — rollback uses `--wait --timeout`; the breaker bounds retries.
- **Redaction still applies** — remediation/escalation events go through the redacting `emit`.
- Commits: author `mittal122`, NO `Co-Authored-By` trailer, no Claude/Anthropic mention. Controller pushes after each task.

## Interfaces locked across tasks

- `rollback.get_revisions(name, ns) -> list[dict]` — each `{"revision": int, "status": str}` from `helm history -o json`; `[]` on error.
- `rollback.previous_good_revision(revisions: list[dict]) -> int | None` — pure; the highest revision below the current one whose status is `deployed`/`superseded`, else `None`.
- `rollback.do_rollback(name, ns, revision: int) -> None` — `helm rollback ... --wait`; raises on failure.
- `remediation.SAFE_AUTO_ACTIONS: set[str]` = `{"rollback"}`; `remediation.is_destructive(action: str) -> bool` — `action not in SAFE_AUTO_ACTIONS` (deny-by-default).
- `breakers.Breaker(max_attempts=2)` — `.record(key)`, `.tripped(key) -> bool`, `.reset(key)`.
- `coordinator.run(cfg, bus, approvals, monitors, breakers) -> None` — adds the Remediate step.
- `POST /rollback {name, namespace, revision?}`.

---

### Task 1: rollback tool

**Files:** Create `backend/tools/rollback.py`, `backend/tests/test_rollback.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_rollback.py
import json
import subprocess
from tools import rollback

def test_previous_good_revision_picks_highest_prior_good():
    revs = [{"revision": 1, "status": "superseded"},
            {"revision": 2, "status": "deployed"}]
    assert rollback.previous_good_revision(revs) == 1

def test_previous_good_revision_skips_failed():
    revs = [{"revision": 1, "status": "superseded"},
            {"revision": 2, "status": "failed"},
            {"revision": 3, "status": "deployed"}]
    assert rollback.previous_good_revision(revs) == 1  # 2 is failed, skip it

def test_previous_good_revision_none_when_only_one():
    assert rollback.previous_good_revision([{"revision": 1, "status": "deployed"}]) is None

def test_previous_good_revision_none_when_empty():
    assert rollback.previous_good_revision([]) is None

def test_get_revisions_parses_history(monkeypatch):
    class _R:
        returncode = 0
        stdout = json.dumps([{"revision": 1, "status": "superseded"},
                             {"revision": 2, "status": "deployed"}])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    assert rollback.get_revisions("demo", "default") == [
        {"revision": 1, "status": "superseded"},
        {"revision": 2, "status": "deployed"}]

def test_get_revisions_empty_on_error(monkeypatch):
    class _R: returncode = 1; stdout = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    assert rollback.get_revisions("demo", "default") == []
```

- [ ] **Step 2: Run to verify fail** — `cd backend && python -m pytest tests/test_rollback.py -v` → FAIL.

- [ ] **Step 3: Write `backend/tools/rollback.py`**

```python
import json
import subprocess

_GOOD = {"deployed", "superseded"}

def get_revisions(name: str, namespace: str) -> list[dict]:
    r = subprocess.run(
        ["helm", "history", name, "-n", namespace, "-o", "json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        items = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    return [{"revision": int(i["revision"]), "status": i["status"]} for i in items]

def previous_good_revision(revisions: list[dict]) -> int | None:
    if not revisions:
        return None
    current = max(r["revision"] for r in revisions)
    candidates = [r["revision"] for r in revisions
                  if r["revision"] < current and r["status"] in _GOOD]
    return max(candidates) if candidates else None

def do_rollback(name: str, namespace: str, revision: int) -> None:
    subprocess.run(
        ["helm", "rollback", name, str(revision), "-n", namespace,
         "--wait", "--timeout", "120s"],
        capture_output=True, text=True, check=True,
    )
```

- [ ] **Step 4: Run to verify pass + full suite** — all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/tools/rollback.py backend/tests/test_rollback.py
git commit -m "feat: rollback tool (helm history + previous-good-revision + helm rollback)"
```

---

### Task 2: destructive-op allowlist + circuit breaker

**Files:** Create `backend/remediation.py`, `backend/breakers.py`, `backend/tests/test_remediation.py`, `backend/tests/test_breakers.py`

- [ ] **Step 1: Write `backend/remediation.py`**

```python
# Deny-by-default: only actions explicitly listed here may auto-run in autonomous
# mode. Deletes / namespace / PVC / CRD ops are NOT here and stay human-gated.
SAFE_AUTO_ACTIONS = {"rollback"}

def is_destructive(action: str) -> bool:
    return action not in SAFE_AUTO_ACTIONS
```

- [ ] **Step 2: Write `backend/breakers.py`**

```python
class Breaker:
    def __init__(self, max_attempts: int = 2) -> None:
        self.max_attempts = max_attempts
        self._counts: dict[str, int] = {}

    def record(self, key: str) -> None:
        self._counts[key] = self._counts.get(key, 0) + 1

    def tripped(self, key: str) -> bool:
        return self._counts.get(key, 0) >= self.max_attempts

    def reset(self, key: str) -> None:
        self._counts.pop(key, None)
```

- [ ] **Step 3: Write the tests**

```python
# backend/tests/test_remediation.py
import remediation

def test_rollback_is_safe():
    assert remediation.is_destructive("rollback") is False

def test_delete_is_destructive():
    assert remediation.is_destructive("delete-namespace") is True
    assert remediation.is_destructive("uninstall") is True
    assert remediation.is_destructive("anything-not-allowlisted") is True
```

```python
# backend/tests/test_breakers.py
from breakers import Breaker

def test_trips_after_max():
    b = Breaker(max_attempts=2)
    assert b.tripped("d") is False
    b.record("d"); assert b.tripped("d") is False
    b.record("d"); assert b.tripped("d") is True

def test_reset_clears():
    b = Breaker(max_attempts=1)
    b.record("d"); assert b.tripped("d") is True
    b.reset("d"); assert b.tripped("d") is False

def test_keys_independent():
    b = Breaker(max_attempts=1)
    b.record("a"); assert b.tripped("a") is True and b.tripped("b") is False
```

- [ ] **Step 4: Run to verify pass + full suite** — all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/remediation.py backend/breakers.py backend/tests/test_remediation.py backend/tests/test_breakers.py
git commit -m "feat: destructive-op allowlist (deny-by-default) and circuit breaker"
```

---

### Task 3: coordinator autonomous auto-remediation (the Remediate step)

**Files:** Modify `backend/coordinator.py`, `backend/tests/test_coordinator.py`

**Interfaces:** `run(cfg, bus, approvals, monitors, breakers)`; on a Verify timeout in autonomous mode, call `remediate(...)` which rolls back to a prior good revision behind the breaker + destructive gate, or escalates.

- [ ] **Step 1: Add imports to `coordinator.py`**

```python
from tools import manifests, validate, deploy, monitor, rollback
import remediation
from breakers import Breaker
```

- [ ] **Step 2: Change the signature** to `async def run(cfg, bus, approvals, monitors, breakers):`

- [ ] **Step 3: Add the `remediate` helper inside `run`** (after `emit`/`explain` are defined). It is deterministic — the rollback target comes from helm history, never from an agent:

```python
    async def remediate(reason):
        rstage = "Remediate"
        await emit("stage_enter", rstage, "Attempting auto-recovery")
        if breakers.tripped(name):
            await emit("escalation", rstage,
                       "Circuit breaker tripped — auto-remediation frozen, human needed")
            await emit("stage_exit", rstage, "Frozen")
            return
        revs = await asyncio.to_thread(rollback.get_revisions, name, ns)
        prior = rollback.previous_good_revision(revs)
        if prior is None:
            await emit("escalation", rstage,
                       "No prior good revision to roll back to — human needed")
            await emit("stage_exit", rstage, "Escalated")
            return
        action = "rollback"
        if remediation.is_destructive(action):   # rollback is safe; guards future actions
            await emit("escalation", rstage,
                       f"Action '{action}' is destructive — human-gated, not auto-run")
            await emit("stage_exit", rstage, "Gated")
            return
        breakers.record(name)
        await emit("remediation", rstage,
                   f"Rolling back {name} to revision {prior} (cause: {reason})",
                   {"revision": prior})
        try:
            await asyncio.to_thread(rollback.do_rollback, name, ns, prior)
            await emit("remediation", rstage,
                       f"Rolled back to revision {prior} — recovered", {"revision": prior})
        except Exception as e:
            await emit("escalation", rstage, f"Rollback failed: {e} — human needed")
        await emit("stage_exit", rstage, "Done")
```

- [ ] **Step 4: Call `remediate` on the Verify timeout.** In the Verify loop's timeout `else:` branch, after emitting the timeout `error` (and the existing failure-explanation), add — only in autonomous mode:

```python
        else:
            failures = await asyncio.to_thread(monitor.detect_failures, name, ns)
            await emit("error", "Verify", "Rollout did not complete in time",
                       {"timeout_s": ROLLOUT_TIMEOUT_S, "failures": failures})
            if mode == "autonomous":
                await remediate("rollout did not complete")
            return
```

(Keep the existing in-loop `explain(f)` calls; those are the LLM advisory path and are unchanged.)

- [ ] **Step 5: Write the tests** (append to `test_coordinator.py`)

```python
import breakers as breakers_mod

def _cfg_auto(**over):
    base = {"name": "app", "image": "i:1", "namespace": "default", "port": 8080,
            "replicas": 1, "mode": "autonomous", "secrets": {}}
    base.update(over); return base

@pytest.mark.asyncio
async def test_autonomous_rollback_on_failure(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (0, 1))  # never ready
    monkeypatch.setattr(coordinator.monitor, "detect_failures", lambda n, ns: [])
    monkeypatch.setattr(coordinator.monitor, "get_logs", lambda n, ns: "")
    monkeypatch.setattr(coordinator.error_resolver, "resolve", lambda ctx: {
        "root_cause": "", "plain_explanation": "", "evidence": [], "recommended_action": "",
        "fix_prompt": "", "auto_remediable": False, "suggested_auto_action": "",
        "severity": "low", "suspicious_input_detected": False})
    monkeypatch.setattr(coordinator.rollback, "get_revisions",
                        lambda n, ns: [{"revision": 1, "status": "superseded"},
                                       {"revision": 2, "status": "deployed"}])
    rolled = {}
    monkeypatch.setattr(coordinator.rollback, "do_rollback",
                        lambda n, ns, rev: rolled.update(rev=rev))
    monkeypatch.setattr(coordinator, "POLL_INTERVAL_S", 2)
    monkeypatch.setattr(coordinator, "ROLLOUT_TIMEOUT_S", 2)
    async def _no_sleep(x): pass
    monkeypatch.setattr(coordinator.asyncio, "sleep", _no_sleep)
    bus = EventBus(); q = bus.subscribe()
    appr = approvals_mod.Approvals(); mons = monitors_mod.Monitors(); brk = breakers_mod.Breaker(max_attempts=2)
    await coordinator.run(_cfg_auto(), bus, appr, mons, brk)
    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "remediation" in types
    assert rolled.get("rev") == 1     # rolled back to the prior good revision

@pytest.mark.asyncio
async def test_no_prior_revision_escalates(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (0, 1))
    monkeypatch.setattr(coordinator.monitor, "detect_failures", lambda n, ns: [])
    monkeypatch.setattr(coordinator.monitor, "get_logs", lambda n, ns: "")
    monkeypatch.setattr(coordinator.error_resolver, "resolve", lambda ctx: {
        "root_cause": "", "plain_explanation": "", "evidence": [], "recommended_action": "",
        "fix_prompt": "", "auto_remediable": False, "suggested_auto_action": "",
        "severity": "low", "suspicious_input_detected": False})
    monkeypatch.setattr(coordinator.rollback, "get_revisions",
                        lambda n, ns: [{"revision": 1, "status": "deployed"}])  # first deploy, nothing prior
    called = {"rb": False}
    monkeypatch.setattr(coordinator.rollback, "do_rollback",
                        lambda n, ns, rev: called.__setitem__("rb", True))
    monkeypatch.setattr(coordinator, "POLL_INTERVAL_S", 2)
    monkeypatch.setattr(coordinator, "ROLLOUT_TIMEOUT_S", 2)
    async def _no_sleep(x): pass
    monkeypatch.setattr(coordinator.asyncio, "sleep", _no_sleep)
    bus = EventBus(); q = bus.subscribe()
    appr = approvals_mod.Approvals(); mons = monitors_mod.Monitors(); brk = breakers_mod.Breaker()
    await coordinator.run(_cfg_auto(), bus, appr, mons, brk)
    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "escalation" in types
    assert called["rb"] is False

@pytest.mark.asyncio
async def test_breaker_tripped_freezes(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (0, 1))
    monkeypatch.setattr(coordinator.monitor, "detect_failures", lambda n, ns: [])
    monkeypatch.setattr(coordinator.monitor, "get_logs", lambda n, ns: "")
    monkeypatch.setattr(coordinator.error_resolver, "resolve", lambda ctx: {
        "root_cause": "", "plain_explanation": "", "evidence": [], "recommended_action": "",
        "fix_prompt": "", "auto_remediable": False, "suggested_auto_action": "",
        "severity": "low", "suspicious_input_detected": False})
    monkeypatch.setattr(coordinator.rollback, "get_revisions",
                        lambda n, ns: [{"revision": 1, "status": "superseded"},
                                       {"revision": 2, "status": "deployed"}])
    monkeypatch.setattr(coordinator.rollback, "do_rollback", lambda n, ns, rev: None)
    monkeypatch.setattr(coordinator, "POLL_INTERVAL_S", 2)
    monkeypatch.setattr(coordinator, "ROLLOUT_TIMEOUT_S", 2)
    async def _no_sleep(x): pass
    monkeypatch.setattr(coordinator.asyncio, "sleep", _no_sleep)
    brk = breakers_mod.Breaker(max_attempts=1)
    brk.record("app")   # already at the limit
    bus = EventBus(); q = bus.subscribe()
    appr = approvals_mod.Approvals(); mons = monitors_mod.Monitors()
    await coordinator.run(_cfg_auto(), bus, appr, mons, brk)
    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "escalation" in types  # frozen by the breaker
```

- [ ] **Step 6: Update ALL existing `coordinator.run(...)` calls** in `test_coordinator.py` to pass a 5th arg `breakers_mod.Breaker()`. Add `import breakers as breakers_mod` at the top. Existing tests that don't reach Remediate are unaffected by the value.

- [ ] **Step 7: Run FULL suite** — `cd backend && timeout 90 python -m pytest -q` — all pass, no hang.

- [ ] **Step 8: Commit**

```bash
git add backend/coordinator.py backend/tests/test_coordinator.py
git commit -m "feat: autonomous auto-rollback on failure, behind circuit breaker and destructive-op gate"
```

---

### Task 4: FastAPI — wire the breaker + manual /rollback endpoint

**Files:** Modify `backend/main.py`, `backend/tests/test_main.py`

- [ ] **Step 1: Write failing tests** (append to `test_main.py`)

```python
def test_rollback_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    called = {}
    monkeypatch.setattr(main.rollback, "do_rollback",
                        lambda n, ns, rev: called.update(n=n, ns=ns, rev=rev))
    client = TestClient(main.app)
    r = client.post("/rollback", json={"name": "demo", "namespace": "default", "revision": 1})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert called == {"n": "demo", "ns": "default", "rev": 1}
```

- [ ] **Step 2: Run to verify fail** — FAIL.

- [ ] **Step 3: Edit `backend/main.py`**

Add imports + the breaker instance:
```python
from tools import rollback
from breakers import Breaker

breakers = Breaker()
```

Pass `breakers` as the 5th arg in the `/deploy` handler's `coordinator_run(...)` call:
```python
    task = asyncio.create_task(coordinator_run(req.model_dump(), bus, approvals, monitors, breakers))
```

Add the endpoint + model:
```python
class RollbackRequest(BaseModel):
    name: str
    namespace: str = "default"
    revision: int

@app.post("/rollback")
async def rollback_endpoint(req: RollbackRequest):
    await asyncio.to_thread(rollback.do_rollback, req.name, req.namespace, req.revision)
    return {"ok": True}
```

- [ ] **Step 4: Run to verify pass + full suite** — all pass. (Update the `test_main.py` `fake_run` deploy stub to accept 5 args if it currently accepts 4.)

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_main.py
git commit -m "feat: wire circuit breaker into deploy, add manual /rollback endpoint"
```

---

### Task 5: UI — remediation/escalation rendering + rollback control

**Files:** Modify `backend/static/index.html`

- [ ] **Step 1: Render `remediation` and `escalation` events** in the SSE `onmessage` (textContent only, NO innerHTML):

```javascript
    if (e.type === "remediation" || e.type === "escalation") {
      const div = document.createElement("div");
      div.className = "ev " + (e.type === "escalation" ? "error" : "endpoint");
      div.textContent = (e.type === "escalation" ? "⚠ " : "↩ ") + e.message;
      log.appendChild(div);
    }
```

- [ ] **Step 2: Add a manual rollback control** near the mode selector:

```html
  <span style="margin-left:8px">
    <input id="rb-rev" type="number" placeholder="rev" style="width:60px">
    <button id="rb-btn" style="background:#f87171">Rollback</button>
  </span>
```

Wire it:
```javascript
  document.getElementById("rb-btn").onclick = () =>
    fetch("/rollback", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({name: currentName, namespace: "default",
                            revision: Number(document.getElementById("rb-rev").value)})});
```

- [ ] **Step 3: Verify** — `cd backend && python -m pytest -q` all pass; `grep -c innerHTML backend/static/index.html` → `0`.

- [ ] **Step 4: Commit**

```bash
git add backend/static/index.html
git commit -m "feat: UI remediation/escalation rendering and manual rollback control"
```

---

### Task 6: End-to-end verification (definition of done)

**Files:** none.

- [ ] **Step 1: Cluster + backend up**

```bash
bash scripts/kind-up.sh && kubectl config use-context kind-helmsman
bash scripts/dev.sh   # one terminal
```

- [ ] **Step 2: Deploy a good v1 (autonomous), confirm healthy**

```bash
curl -s -X POST localhost:8000/deploy -H 'Content-Type: application/json' \
  -d '{"name":"demo","image":"mendhak/http-https-echo:31","port":8080,"replicas":1,"mode":"autonomous"}'
```
Wait for `endpoint`. Confirm: `helm history demo -n default` shows revision 1 `deployed`.

- [ ] **Step 3: Deploy a bad v2 (upgrade) → auto-rollback (the headline)**

```bash
curl -s -X POST localhost:8000/deploy -H 'Content-Type: application/json' \
  -d '{"name":"demo","image":"mendhak/http-https-echo:doesnotexist999","port":8080,"replicas":1,"mode":"autonomous"}'
```
Expected in the stream: `failure` (ErrImagePull) → `error` (Verify timeout) → `stage_enter Remediate` → `remediation` "Rolling back demo to revision 1" → `remediation` "recovered". Then confirm the app is back on the good image:
```bash
kubectl get deploy demo -o jsonpath='{.spec.template.spec.containers[0].image}'   # → mendhak/http-https-echo:31
kubectl get pods -l app.kubernetes.io/name=demo   # Running
helm history demo -n default   # a new revision that is a rollback to 1
```

- [ ] **Step 4: First-deploy failure escalates (no prior revision)**

```bash
curl -s -X POST localhost:8000/deploy -H 'Content-Type: application/json' \
  -d '{"name":"fresh","image":"mendhak/http-https-echo:nope999","port":8080,"replicas":1,"mode":"autonomous"}'
```
Expected: `escalation` "No prior good revision to roll back to — human needed"; NO rollback attempted.

- [ ] **Step 5: Manual rollback endpoint**

```bash
curl -s -X POST localhost:8000/rollback -H 'Content-Type: application/json' \
  -d '{"name":"demo","namespace":"default","revision":1}'
```
Expected: `{"ok": true}` and `helm history demo` shows another rollback revision.

- [ ] **Step 6: Confirm destructive ops never auto-run** — `remediation.is_destructive("delete-namespace")` is `True`, and the coordinator only ever calls `rollback.do_rollback` (grep: no `kubectl delete` / `helm uninstall` in the auto path). Document.

- [ ] **Step 7: Cleanup** — `helm uninstall demo fresh -n default 2>/dev/null || true`

---

## Self-review notes

- **Spec coverage:** autonomous auto-recovery via rollback (§5) ✔; circuit breaker (max attempts → freeze + escalate) (§5) ✔; destructive-op gate deny-by-default, deletes human-gated even in autonomous (§5/§7) ✔; rollback via `helm rollback`/`helm history`, not rebuilt (CLAUDE.md #4) ✔; deterministic core — rollback target from helm history, agent output never executed (§7.2) ✔; redaction via emit ✔; never hang (rollback `--wait --timeout`, breaker bounds retries) ✔; manual `/rollback` for human control ✔.
- **Deferred:** progressive rollout / canary (later); acting on the LLM's `suggested_auto_action` text (deliberately never — deterministic only); cloud/multi-tenant (Phase 5).
- **Type consistency:** `run(cfg, bus, approvals, monitors, breakers)` used identically in main and tests; `rollback.get_revisions/previous_good_revision/do_rollback`, `remediation.is_destructive`, `Breaker.record/tripped/reset` consistent across tasks.
