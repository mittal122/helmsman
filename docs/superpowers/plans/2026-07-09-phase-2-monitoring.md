# Phase 2 — Monitoring (lightweight) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** After a deploy is verified, continuously monitor it: detect failures (CrashLoopBackOff / ImagePullBackOff / OOMKilled / Pending) deterministically from pod status + events, stream health + CPU/memory metrics + logs to the UI, and let the user stop monitoring. Lightweight: metrics-server + kubectl, no Prometheus/Loki.

**Architecture:** A new `Monitor` coordinator stage runs after `Verify`: a stoppable poll loop that each cycle calls deterministic tools (`monitor.detect_failures`, `monitor.get_metrics`) and emits `health`/`failure` events through the existing redacting `emit` choke point. Failure detection is a pure function over `kubectl get pods -o json`, so it's fully unit-testable without a cluster. A `Monitors` registry holds per-deployment stop flags (resolved by `POST /monitor/stop`); a safety cycle cap prevents an unbounded background task.

**Tech Stack:** Python 3.12, FastAPI, pytest; kubectl, metrics-server, kind.

## Global Constraints

- **Deterministic core, NO LLM** (LLM root-cause is Phase 3).
- **Cluster text is untrusted data** — failures/logs/metrics are displayed, never executed or acted on. (Spec §7.2)
- **Secret redaction still applies** — Monitor events go through the same `emit` choke point (already redacts).
- **Detect, don't assume** — if metrics-server is absent, metrics degrade to empty, monitoring still runs. (Spec §13)
- **Never hang** — the Monitor loop is bounded by a stop signal AND a max-cycle safety cap.
- Commits: author `mittal122`, **NO `Co-Authored-By` trailer**, no Claude/Anthropic mention. Controller pushes after each task.

## Interfaces locked across tasks

- `monitor._failures_from_pods(items: list) -> list[dict]` — pure; each `{pod, container, type, message}`.
- `monitor.detect_failures(name: str, namespace: str) -> list[dict]` — shells out, returns `[]` on error.
- `monitor.get_metrics(name: str, namespace: str) -> list[dict]` — `[{pod, cpu, memory}]`; `[]` if metrics-server absent.
- `monitor.get_logs(name: str, namespace: str, tail: int = 20) -> str`.
- `monitors.Monitors` — `.start(key)`, `.stop(key)`, `.is_stopped(key) -> bool`.
- `coordinator.run(cfg, bus, approvals, monitors) -> None` — adds the Monitor stage.
- `POST /monitor/stop {name}` — sets the stop flag.

---

### Task 1: metrics-server install script

**Files:** Create `scripts/monitoring-up.sh`

- [ ] **Step 1: Write `scripts/monitoring-up.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
# metrics-server for kubectl top. On kind, kubelet serving certs aren't signed
# by the cluster CA, so metrics-server needs --kubelet-insecure-tls.
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl patch -n kube-system deployment metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
kubectl -n kube-system rollout status deployment metrics-server --timeout=120s
echo "metrics-server ready"
```

- [ ] **Step 2: Install and verify**

```bash
chmod +x scripts/monitoring-up.sh
bash scripts/monitoring-up.sh
sleep 20 && kubectl top nodes
```
Expected: `metrics-server ready`, and `kubectl top nodes` prints CPU/memory for the node (may take ~15-30s after rollout for first metrics).

- [ ] **Step 3: Commit**

```bash
git add scripts/monitoring-up.sh
git commit -m "feat: metrics-server install script for kind"
```

---

### Task 2: Failure detection (pure + shell)

**Files:** Create `backend/tools/monitor.py`, `backend/tests/test_monitor.py`

**Interfaces:** `_failures_from_pods(items)`, `detect_failures(name, namespace)`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_monitor.py
import json
import subprocess
from tools import monitor

def _pod(name, phase="Running", waiting=None, last_term=None):
    cs = {"name": "app", "state": {}, "lastState": {}}
    if waiting:
        cs["state"] = {"waiting": {"reason": waiting, "message": "boom"}}
    if last_term:
        cs["lastState"] = {"terminated": {"reason": last_term}}
    pod = {"metadata": {"name": name}, "status": {"phase": phase, "containerStatuses": [cs]}}
    return pod

def test_detects_crashloop_and_imagepull():
    items = [_pod("a", waiting="CrashLoopBackOff"), _pod("b", waiting="ImagePullBackOff")]
    out = monitor._failures_from_pods(items)
    types = {f["type"] for f in out}
    assert types == {"CrashLoopBackOff", "ImagePullBackOff"}
    assert all(f["pod"] in ("a", "b") for f in out)

def test_detects_oomkilled():
    out = monitor._failures_from_pods([_pod("a", last_term="OOMKilled")])
    assert out[0]["type"] == "OOMKilled"

def test_detects_pending_without_container_statuses():
    pod = {"metadata": {"name": "p"}, "status": {"phase": "Pending"}}
    out = monitor._failures_from_pods([pod])
    assert out[0]["type"] == "Pending"

def test_healthy_pod_no_failures():
    assert monitor._failures_from_pods([_pod("a")]) == []

def test_detect_failures_returns_empty_on_kubectl_error(monkeypatch):
    class _R: returncode = 1; stdout = ""; stderr = "nope"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    assert monitor.detect_failures("x", "default") == []
```

- [ ] **Step 2: Run to verify fail**

Run: `cd backend && python -m pytest tests/test_monitor.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Write `backend/tools/monitor.py`**

```python
import json
import subprocess

_FAIL_WAITING = {
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
    "CreateContainerConfigError", "CreateContainerError", "InvalidImageName",
}

def _failures_from_pods(items: list) -> list[dict]:
    out: list[dict] = []
    for pod in items:
        name = pod.get("metadata", {}).get("name", "?")
        st = pod.get("status", {})
        css = st.get("containerStatuses") or []
        for cs in css:
            waiting = (cs.get("state") or {}).get("waiting") or {}
            if waiting.get("reason") in _FAIL_WAITING:
                out.append({"pod": name, "container": cs.get("name"),
                            "type": waiting["reason"], "message": waiting.get("message", "")})
            term = (cs.get("lastState") or {}).get("terminated") or {}
            if term.get("reason") == "OOMKilled":
                out.append({"pod": name, "container": cs.get("name"),
                            "type": "OOMKilled", "message": "container was OOM-killed"})
        if st.get("phase") == "Pending" and not css:
            out.append({"pod": name, "container": None,
                        "type": "Pending", "message": "pod is pending (unscheduled or waiting)"})
    return out

def detect_failures(name: str, namespace: str) -> list[dict]:
    r = subprocess.run(
        ["kubectl", "get", "pods", "-l", f"app.kubernetes.io/name={name}",
         "-n", namespace, "-o", "json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return []
    items = json.loads(r.stdout).get("items", [])
    return _failures_from_pods(items)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd backend && python -m pytest tests/test_monitor.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/tools/monitor.py backend/tests/test_monitor.py
git commit -m "feat: deterministic pod-failure detection (crashloop/imagepull/oom/pending)"
```

---

### Task 3: Metrics + logs

**Files:** Modify `backend/tools/monitor.py`, `backend/tests/test_monitor.py`

**Interfaces:** `get_metrics(name, ns)`, `get_logs(name, ns, tail)`.

- [ ] **Step 1: Write failing tests** (append to `test_monitor.py`)

```python
class _Run:
    def __init__(self, rc, out=""): self.returncode, self.stdout, self.stderr = rc, out, ""

def test_get_metrics_parses_top(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
        lambda *a, **k: _Run(0, "demo-abc 5m 40Mi\ndemo-def 3m 38Mi\n"))
    m = monitor.get_metrics("demo", "default")
    assert m == [{"pod": "demo-abc", "cpu": "5m", "memory": "40Mi"},
                 {"pod": "demo-def", "cpu": "3m", "memory": "38Mi"}]

def test_get_metrics_empty_when_unavailable(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Run(1))
    assert monitor.get_metrics("demo", "default") == []

def test_get_logs_returns_stdout(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Run(0, "line1\nline2"))
    assert monitor.get_logs("demo", "default") == "line1\nline2"
```

- [ ] **Step 2: Run to verify fail**

Run: `cd backend && python -m pytest tests/test_monitor.py -k "metrics or logs" -v`
Expected: FAIL.

- [ ] **Step 3: Append to `monitor.py`**

```python
def get_metrics(name: str, namespace: str) -> list[dict]:
    r = subprocess.run(
        ["kubectl", "top", "pods", "-l", f"app.kubernetes.io/name={name}",
         "-n", namespace, "--no-headers"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return []
    rows: list[dict] = []
    for ln in r.stdout.splitlines():
        parts = ln.split()
        if len(parts) >= 3:
            rows.append({"pod": parts[0], "cpu": parts[1], "memory": parts[2]})
    return rows

def get_logs(name: str, namespace: str, tail: int = 20) -> str:
    r = subprocess.run(
        ["kubectl", "logs", "-l", f"app.kubernetes.io/name={name}",
         "-n", namespace, "--tail", str(tail), "--all-containers", "--prefix"],
        capture_output=True, text=True,
    )
    return r.stdout if r.returncode == 0 else ""
```

- [ ] **Step 4: Run to verify pass**

Run: `cd backend && python -m pytest tests/test_monitor.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/tools/monitor.py backend/tests/test_monitor.py
git commit -m "feat: monitor metrics (kubectl top) and logs (kubectl logs)"
```

---

### Task 4: Monitors registry + Monitor stage in coordinator

**Files:** Create `backend/monitors.py`, `backend/tests/test_monitors.py`; Modify `backend/coordinator.py`, `backend/tests/test_coordinator.py`

**Interfaces:** `Monitors.start/stop/is_stopped`; `coordinator.run(cfg, bus, approvals, monitors)`.

- [ ] **Step 1: Write `backend/monitors.py`**

```python
class Monitors:
    def __init__(self) -> None:
        self._stopped: set[str] = set()

    def start(self, key: str) -> None:
        self._stopped.discard(key)

    def stop(self, key: str) -> None:
        self._stopped.add(key)

    def is_stopped(self, key: str) -> bool:
        return key in self._stopped
```

- [ ] **Step 2: Write `backend/tests/test_monitors.py`**

```python
from monitors import Monitors

def test_stop_then_is_stopped():
    m = Monitors()
    m.start("d"); assert m.is_stopped("d") is False
    m.stop("d");  assert m.is_stopped("d") is True

def test_start_clears_stop():
    m = Monitors()
    m.stop("d"); m.start("d")
    assert m.is_stopped("d") is False
```

- [ ] **Step 3: Write failing coordinator test** (append to `test_coordinator.py`)

```python
import monitors as monitors_mod

@pytest.mark.asyncio
async def test_monitor_stage_emits_health_and_stops(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.monitor, "detect_failures",
                        lambda n, ns: [{"pod": "p", "container": "app",
                                        "type": "CrashLoopBackOff", "message": "x"}])
    monkeypatch.setattr(coordinator.monitor, "get_metrics",
                        lambda n, ns: [{"pod": "p", "cpu": "5m", "memory": "40Mi"}])
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    bus = EventBus(); q = bus.subscribe()
    appr = approvals_mod.Approvals(); mons = monitors_mod.Monitors()
    mons.stop("app")   # stop immediately so the loop runs exactly one cycle then exits
    await coordinator.run(_cfg(mode="autonomous"), bus, appr, mons)
    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "health" in types
    assert "failure" in types      # the injected CrashLoopBackOff surfaced
```

Note: `mons.stop("app")` is set before `run`; the Monitor loop checks `is_stopped` at the TOP of each cycle, so it must run one full cycle (emit health) before exiting only if the check is at the loop end. Implement the loop so it emits at least one snapshot before honoring the stop (see Step 5).

- [ ] **Step 4: Run to verify fail**

Run: `cd backend && python -m pytest tests/test_coordinator.py -k monitor tests/test_monitors.py -v`
Expected: FAIL.

- [ ] **Step 5: Edit `backend/coordinator.py`** — add imports, constants, the `monitors` param, and the Monitor stage.

Add to imports:
```python
from tools import manifests, validate, deploy, monitor
from monitors import Monitors
```

Add constants near the others:
```python
MONITOR_INTERVAL_S = 5
MONITOR_MAX_CYCLES = 720   # safety cap (~1h at 5s); real stop is the Monitors flag
```

Change the signature:
```python
async def run(cfg: dict, bus: EventBus, approvals: Approvals, monitors: Monitors) -> None:
```

Replace the final `await emit("stage_exit", "Verify", "Done")` line with the Verify exit followed by the Monitor stage (insert before the `except`):
```python
        await emit("stage_exit", "Verify", "Done")

        # Monitor (continuous, stoppable)
        current = "Monitor"
        await emit("stage_enter", "Monitor", "Monitoring deployment")
        monitors.start(name)
        for _ in range(MONITOR_MAX_CYCLES):
            failures = await asyncio.to_thread(monitor.detect_failures, name, ns)
            metrics = await asyncio.to_thread(monitor.get_metrics, name, ns)
            await emit("health", "Monitor", "Health snapshot",
                       {"failures": failures, "metrics": metrics})
            for f in failures:
                await emit("failure", "Monitor", f"{f['type']} on {f['pod']}", f)
            if monitors.is_stopped(name):
                break
            await asyncio.sleep(MONITOR_INTERVAL_S)
        await emit("stage_exit", "Monitor", "Monitoring stopped")
```

(The stop check is AFTER the first emit, so a pre-set stop still yields exactly one health snapshot — matching the test.)

- [ ] **Step 6: Update existing coordinator tests for the new signature**

Every `coordinator.run(...)` call in `test_coordinator.py` now needs a 4th arg. For the existing autonomous/manual tests, pass `monitors_mod.Monitors()` after pre-calling `.stop(name)` so their Monitor loop runs one cycle and exits (otherwise they loop). Add `import monitors as monitors_mod` at the top if not present. Update the happy-path/redaction/timeout/reject tests: construct `mons = monitors_mod.Monitors(); mons.stop("app")` and pass as the 4th arg. (For the validation-failure and reject tests, the Monitor stage is never reached, so a plain `Monitors()` is fine.)

- [ ] **Step 7: Run full suite**

Run: `cd backend && python -m pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add backend/monitors.py backend/coordinator.py backend/tests/test_monitors.py backend/tests/test_coordinator.py
git commit -m "feat: stoppable continuous Monitor stage emitting health/failure snapshots"
```

---

### Task 5: FastAPI — wire monitors + /monitor/stop

**Files:** Modify `backend/main.py`, `backend/tests/test_main.py`

- [ ] **Step 1: Write failing tests** (append to `test_main.py`)

```python
def test_monitor_stop(monkeypatch):
    from fastapi.testclient import TestClient
    called = {}
    monkeypatch.setattr(main.monitors, "stop", lambda k: called.setdefault("k", k))
    client = TestClient(main.app)
    r = client.post("/monitor/stop", json={"name": "demo"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert called["k"] == "demo"
```

- [ ] **Step 2: Run to verify fail**

Run: `cd backend && python -m pytest tests/test_main.py -k monitor_stop -v`
Expected: FAIL.

- [ ] **Step 3: Edit `backend/main.py`**

Add import + instance:
```python
from monitors import Monitors
monitors = Monitors()
```

Update the deploy handler's coordinator call to pass `monitors` (4th arg):
```python
    task = asyncio.create_task(coordinator_run(req.model_dump(), bus, approvals, monitors))
```

Add the endpoint + request model:
```python
class MonitorStopRequest(BaseModel):
    name: str

@app.post("/monitor/stop")
async def monitor_stop(req: MonitorStopRequest):
    monitors.stop(req.name)
    return {"ok": True}
```

- [ ] **Step 4: Run full suite**

Run: `cd backend && python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_main.py
git commit -m "feat: wire monitors into deploy, add /monitor/stop endpoint"
```

---

### Task 6: UI — health, metrics, failures, stop button

**Files:** Modify `backend/static/index.html`

- [ ] **Step 1: Add a monitoring panel** after the log div:

```html
  <div id="monitor" style="display:none;margin-top:12px;padding:10px;border:1px solid #25323d;border-radius:8px">
    <b>Monitoring</b> <button id="btn-stop" style="background:#f87171;float:right">Stop</button>
    <div id="failures" style="color:#f87171;margin:6px 0"></div>
    <div id="metrics" style="font-family:monospace;font-size:.8rem;color:#93a4b3"></div>
  </div>
```

- [ ] **Step 2: Handle health/failure events in the SSE `onmessage`** (add after the existing approval handling — build DOM with textContent only, NO innerHTML):

```javascript
    if (e.type === "health") {
      document.getElementById("monitor").style.display = "block";
      const met = document.getElementById("metrics");
      met.textContent = (e.data.metrics || []).map(m => `${m.pod}  cpu ${m.cpu}  mem ${m.memory}`).join("\n");
      const fl = document.getElementById("failures");
      fl.textContent = (e.data.failures || []).map(f => `⚠ ${f.type} on ${f.pod}`).join("\n");
    }
```

- [ ] **Step 3: Wire the Stop button** (in the script, near the approve buttons):

```javascript
  document.getElementById("btn-stop").onclick = () =>
    fetch("/monitor/stop", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({name: currentName})});
```

- [ ] **Step 4: Verify**

Run: `cd backend && python -m pytest -q` (all pass) and `grep -c innerHTML backend/static/index.html` → `0`.

- [ ] **Step 5: Commit**

```bash
git add backend/static/index.html
git commit -m "feat: UI monitoring panel (health, metrics, failures, stop)"
```

---

### Task 7: End-to-end verification (definition of done)

**Files:** none.

- [ ] **Step 1: Cluster + metrics-server + backend**

```bash
bash scripts/kind-up.sh && kubectl config use-context kind-helmsman
bash scripts/monitoring-up.sh
bash scripts/dev.sh   # one terminal
```

- [ ] **Step 2: Deploy a healthy app (autonomous), confirm monitoring starts**

```bash
curl -s -X POST localhost:8000/deploy -H 'Content-Type: application/json' \
  -d '{"name":"demo","image":"mendhak/http-https-echo:31","port":8080,"replicas":2,"mode":"autonomous"}'
```
Watch `GET /events` (or the UI): after `endpoint`, expect `stage_enter Monitor`, then recurring `health` snapshots. After metrics-server warms up (~30s), the health event's `metrics` array is non-empty (cpu/mem per pod).

- [ ] **Step 3: Induce a real failure and confirm detection**

```bash
curl -s -X POST localhost:8000/deploy -H 'Content-Type: application/json' \
  -d '{"name":"broken","image":"mendhak/http-https-echo:doesnotexist999","port":8080,"replicas":1,"mode":"autonomous"}'
```
Expected: the pipeline deploys (schema/dry-run pass — image existence isn't checked), rollout times out at Verify, OR if monitoring is reached, a `failure` event of type `ImagePullBackOff`/`ErrImagePull` on the `broken` pod is emitted. Confirm the failure type appears in the stream and `kubectl get pods -l app.kubernetes.io/name=broken` shows `ImagePullBackOff`.

- [ ] **Step 4: Stop monitoring**

```bash
curl -s -X POST localhost:8000/monitor/stop -H 'Content-Type: application/json' -d '{"name":"demo"}'
```
Expected: within one interval, a `stage_exit Monitor "Monitoring stopped"` event; no further `health` snapshots for `demo`.

- [ ] **Step 5: Cleanup**

```bash
helm uninstall demo broken -n default 2>/dev/null || true
```

---

## Self-review notes

- **Spec coverage:** deterministic failure detection from pod status/events (§4 Monitor) ✔; metrics via metrics-server, logs via kubectl (§10 updated) ✔; continuous stoppable Monitor stage streaming to UI (§4 step 9) ✔; detect-don't-assume (metrics degrade to empty) ✔; untrusted cluster text only displayed ✔; redaction still applies via emit ✔; never-hang (stop flag + max-cycle cap) ✔.
- **Deferred:** Prometheus/Loki (history, alert rules, dashboards) to cloud phase; auto-remediation on detected failure to Phase 4; LLM root-cause explanation to Phase 3.
- **Type consistency:** `coordinator.run(cfg, bus, approvals, monitors)` used identically in main and tests; `Monitors.start/stop/is_stopped`, `monitor.detect_failures/get_metrics/get_logs`, and the `{pod, container, type, message}` failure shape are consistent across tasks.
