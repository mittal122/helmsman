# Phase 0 — Walking Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the transparent deployment pipeline end-to-end on a local `kind` cluster — take a container image + config, render Helm manifests, validate them, deploy, stream every step live, verify readiness, and return the access endpoint. No LLM.

**Architecture:** A FastAPI backend runs a linear async state machine (the coordinator): `Generate → Validate → Deploy → Verify`. Deterministic tools (thin wrappers over `helm`, `kubeconform`, `kubectl`) do the work and return data; the coordinator narrates by publishing typed events to an in-memory bus. A single SSE endpoint streams those events to a minimal browser UI. One fixed Helm chart renders from a generated `values.yaml`.

**Tech Stack:** Python 3.12, FastAPI + uvicorn, PyYAML, pytest + pytest-asyncio; Helm, kubeconform, kubectl, kind, Docker.

## Global Constraints

- **Deterministic core, thin LLM shell** — Phase 0 has NO LLM. (CLAUDE.md #1)
- **LLM never writes final YAML** — manifests come from the fixed Helm chart, rendered from validated inputs. (CLAUDE.md #2)
- **Manifest generation = Helm**, one fixed chart in `chart/` + generated `values.yaml`. Never generate charts. (CLAUDE.md #4)
- **No LangChain/LangGraph.** Coordinator = plain Python FSM. (CLAUDE.md #8)
- **Transparency is architectural** — every stage/tool step emits a typed event; the UI is a pure subscriber. (Spec §3.1)
- **Validate before touching the cluster** — kubeconform + `kubectl apply --dry-run=server` gate every deploy. (Spec §4 step 5)
- **Verify before declaring success** — rollout complete AND readiness before returning the endpoint. (Spec §4 step 8)
- **Generated manifests carry best-practice defaults** — requests/limits, liveness+readiness+startup probes, securityContext (runAsNonRoot, readOnlyRootFilesystem, drop ALL caps), rollout strategy, pinned image tag, standard labels. (Spec §6)
- **Rollout watch has a timeout** — never hang; surface events on timeout. (Spec §13)
- Repo layout per spec §15.4: `backend/`, `chart/`, `scripts/`.
- Commit messages end with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

---

## File structure

```
backend/
  requirements.txt        Python deps
  main.py                 FastAPI app: POST /deploy, GET /events (SSE), GET / (UI)
  events.py               Event dataclass + async EventBus (pub/sub)
  coordinator.py          async FSM: Generate→Validate→Deploy→Verify; the ONLY emitter
  tools/
    __init__.py
    manifests.py          build_values(cfg) + render(cfg) -> rendered YAML via `helm template`
    validate.py           validate(manifests, ns) -> (ok, issues) via kubeconform + kubectl dry-run
    deploy.py             install(cfg), get_replicas(name,ns), get_endpoint(name,ns)
  static/
    index.html            minimal SSE subscriber UI
  tests/
    test_events.py
    test_coordinator.py
    test_manifests.py
    test_validate.py
chart/
  Chart.yaml
  values.yaml             defaults (best-practice §6)
  templates/
    _helpers.tpl          shared labels
    deployment.yaml       §6-compliant Deployment
    service.yaml          ClusterIP Service
scripts/
  kind-up.sh              create local kind cluster "helmsman"
  dev.sh                  run uvicorn
```

**Interfaces locked across tasks (tools are sync; coordinator is async and emits):**
- `events.Event(type: str, stage: str, message: str, data: dict = {}, ts: float = now)`
- `events.EventBus.subscribe() -> asyncio.Queue`, `.unsubscribe(q)`, `async .publish(Event)`
- `manifests.build_values(cfg: dict) -> dict`
- `manifests.render(cfg: dict) -> str`  (rendered multi-doc YAML)
- `validate.validate(manifests: str, namespace: str) -> tuple[bool, list[str]]`
- `deploy.install(cfg: dict) -> None`  (raises `subprocess.CalledProcessError` on failure)
- `deploy.get_replicas(name: str, namespace: str) -> tuple[int, int]`  (ready, desired)
- `deploy.get_endpoint(name: str, namespace: str) -> dict`  (`{service, port, port_forward}`)
- `coordinator.run(cfg: dict, bus: EventBus) -> None`  (async; publishes all events)

`cfg` shape (Phase 0): `{"name": str, "image": str, "namespace": str, "port": int, "replicas": int}`.

---

### Task 1: Tooling, kind cluster, and project scaffold

**Files:**
- Create: `backend/requirements.txt`
- Create: `scripts/kind-up.sh`
- Create: `scripts/dev.sh`
- Create: `backend/tools/__init__.py` (empty)

**Interfaces:**
- Consumes: nothing.
- Produces: a running `kind` cluster named `helmsman`; `helm` + `kubeconform` on PATH; Python venv with deps.

- [ ] **Step 1: Install helm and kubeconform**

```bash
# helm
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
# kubeconform
curl -fsSL https://github.com/yannh/kubeconform/releases/latest/download/kubeconform-linux-amd64.tar.gz \
  | tar xz -C /tmp && sudo mv /tmp/kubeconform /usr/local/bin/
```

- [ ] **Step 2: Verify tools**

Run: `helm version --short && kubeconform -v`
Expected: helm v3.x and a kubeconform version print, no errors.

- [ ] **Step 3: Write `scripts/kind-up.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
kind get clusters | grep -qx helmsman || kind create cluster --name helmsman
kubectl cluster-info --context kind-helmsman
```

- [ ] **Step 4: Write `scripts/dev.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../backend"
uvicorn main:app --reload --port 8000
```

- [ ] **Step 5: Write `backend/requirements.txt`**

```
fastapi==0.115.*
uvicorn[standard]==0.32.*
pyyaml==6.*
pytest==8.*
pytest-asyncio==0.24.*
```

- [ ] **Step 6: Create venv, install, bring up cluster**

```bash
chmod +x scripts/*.sh
python3 -m venv .venv && . .venv/bin/activate && pip install -r backend/requirements.txt
touch backend/tools/__init__.py
bash scripts/kind-up.sh
```
Expected: `kubectl cluster-info` shows the control plane running on `kind-helmsman`.

- [ ] **Step 7: Commit**

```bash
git add backend/requirements.txt scripts/ backend/tools/__init__.py
git commit -m "chore: phase 0 scaffold, kind cluster, tooling"
```

---

### Task 2: Event bus

**Files:**
- Create: `backend/events.py`
- Test: `backend/tests/test_events.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Event`, `EventBus` (as locked above). Used by coordinator (publisher) and main (SSE subscriber).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_events.py
import asyncio
import pytest
from events import Event, EventBus

@pytest.mark.asyncio
async def test_subscriber_receives_published_event():
    bus = EventBus()
    q = bus.subscribe()
    await bus.publish(Event(type="stage_enter", stage="Deploy", message="deploying"))
    got = await asyncio.wait_for(q.get(), timeout=1)
    assert got.type == "stage_enter"
    assert got.stage == "Deploy"
    assert got.message == "deploying"

@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery():
    bus = EventBus()
    q = bus.subscribe()
    bus.unsubscribe(q)
    await bus.publish(Event(type="x", stage="s", message="m"))
    assert q.empty()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_events.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'events'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/events.py
import asyncio
import time
from dataclasses import dataclass, field, asdict
from typing import Any

@dataclass
class Event:
    type: str
    stage: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

class EventBus:
    def __init__(self) -> None:
        self._subs: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    async def publish(self, event: Event) -> None:
        for q in list(self._subs):
            await q.put(event)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_events.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/events.py backend/tests/test_events.py
git commit -m "feat: event bus with async pub/sub"
```

---

### Task 3: Helm chart (best-practice defaults)

**Files:**
- Create: `chart/Chart.yaml`
- Create: `chart/values.yaml`
- Create: `chart/templates/_helpers.tpl`
- Create: `chart/templates/deployment.yaml`
- Create: `chart/templates/service.yaml`

**Interfaces:**
- Consumes: nothing.
- Produces: a renderable chart. `helm template <name> chart -f values.yaml` yields a Deployment + Service that pass kubeconform. Used by `manifests.render` and `deploy.install`.

- [ ] **Step 1: Write `chart/Chart.yaml`**

```yaml
apiVersion: v2
name: helmsman-app
description: Fixed chart Helmsman renders per deployment
type: application
version: 0.1.0
appVersion: "0.1.0"
```

- [ ] **Step 2: Write `chart/values.yaml` (best-practice defaults, §6)**

```yaml
name: app
image: mendhak/http-https-echo:31
port: 8080
replicas: 2
probePath: /
resources:
  requests:
    cpu: 50m
    memory: 64Mi
  limits:
    cpu: 500m
    memory: 256Mi
```

- [ ] **Step 3: Write `chart/templates/_helpers.tpl`**

```
{{- define "app.labels" -}}
app.kubernetes.io/name: {{ .Values.name }}
app.kubernetes.io/managed-by: helmsman
{{- end -}}
```

- [ ] **Step 4: Write `chart/templates/deployment.yaml` (§6 compliant)**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Values.name }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.replicas }}
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: {{ .Values.name }}
  template:
    metadata:
      labels:
        {{- include "app.labels" . | nindent 8 }}
    spec:
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: {{ .Values.name }}
          image: {{ .Values.image | quote }}
          ports:
            - containerPort: {{ .Values.port }}
          resources:
            requests:
              cpu: {{ .Values.resources.requests.cpu | quote }}
              memory: {{ .Values.resources.requests.memory | quote }}
            limits:
              cpu: {{ .Values.resources.limits.cpu | quote }}
              memory: {{ .Values.resources.limits.memory | quote }}
          livenessProbe:
            httpGet:
              path: {{ .Values.probePath | quote }}
              port: {{ .Values.port }}
            initialDelaySeconds: 5
            periodSeconds: 10
          readinessProbe:
            httpGet:
              path: {{ .Values.probePath | quote }}
              port: {{ .Values.port }}
            initialDelaySeconds: 3
            periodSeconds: 5
          startupProbe:
            httpGet:
              path: {{ .Values.probePath | quote }}
              port: {{ .Values.port }}
            failureThreshold: 30
            periodSeconds: 2
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: tmp
          emptyDir: {}
```

- [ ] **Step 5: Write `chart/templates/service.yaml`**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: {{ .Values.name }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: {{ .Values.name }}
  ports:
    - port: {{ .Values.port }}
      targetPort: {{ .Values.port }}
```

- [ ] **Step 6: Verify the chart renders and passes schema validation**

Run:
```bash
helm template app chart -f chart/values.yaml | kubeconform -strict -summary -
```
Expected: kubeconform summary reports 0 errors.

- [ ] **Step 7: Commit**

```bash
git add chart/
git commit -m "feat: fixed helm chart with best-practice defaults"
```

---

### Task 4: Manifests tool

**Files:**
- Create: `backend/tools/manifests.py`
- Test: `backend/tests/test_manifests.py`

**Interfaces:**
- Consumes: the chart from Task 3, `cfg` dict.
- Produces: `build_values(cfg) -> dict`, `render(cfg) -> str`.

- [ ] **Step 1: Write the failing test (pure function first)**

```python
# backend/tests/test_manifests.py
from tools import manifests

def test_build_values_maps_config():
    cfg = {"name": "orders", "image": "orders:1.0", "port": 3000, "replicas": 4}
    v = manifests.build_values(cfg)
    assert v["name"] == "orders"
    assert v["image"] == "orders:1.0"
    assert v["port"] == 3000
    assert v["replicas"] == 4

def test_build_values_defaults():
    v = manifests.build_values({"name": "x", "image": "x:1"})
    assert v["replicas"] == 2
    assert v["port"] == 8080
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_manifests.py -v`
Expected: FAIL with `ModuleNotFoundError` / `AttributeError`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/tools/manifests.py
import os
import subprocess
import tempfile
import yaml

CHART_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "chart"))

def build_values(cfg: dict) -> dict:
    return {
        "name": cfg["name"],
        "image": cfg["image"],
        "port": int(cfg.get("port", 8080)),
        "replicas": int(cfg.get("replicas", 2)),
    }

def render(cfg: dict) -> str:
    values = build_values(cfg)
    ns = cfg.get("namespace", "default")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
        vfile = f.name
    try:
        out = subprocess.run(
            ["helm", "template", values["name"], CHART_DIR, "-f", vfile, "--namespace", ns],
            capture_output=True, text=True, check=True,
        )
        return out.stdout
    finally:
        os.unlink(vfile)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_manifests.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Add and run an integration check for render**

```python
# append to backend/tests/test_manifests.py
import shutil, pytest

@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_render_produces_deployment_and_service():
    out = manifests.render({"name": "demo", "image": "nginx:1.27", "port": 8080})
    assert "kind: Deployment" in out
    assert "kind: Service" in out
    assert "demo" in out
```

Run: `cd backend && python -m pytest tests/test_manifests.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add backend/tools/manifests.py backend/tests/test_manifests.py
git commit -m "feat: manifests tool renders values via helm template"
```

---

### Task 5: Validation tool

**Files:**
- Create: `backend/tools/validate.py`
- Test: `backend/tests/test_validate.py`

**Interfaces:**
- Consumes: rendered manifests string, namespace.
- Produces: `validate(manifests, namespace) -> (ok, issues)`.

- [ ] **Step 1: Write the failing test (mock subprocess)**

```python
# backend/tests/test_validate.py
import subprocess
from tools import validate

class _R:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err

def test_valid_manifests_pass(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R(0))
    ok, issues = validate.validate("kind: Deployment", "default")
    assert ok is True
    assert issues == []

def test_schema_failure_reported(monkeypatch):
    calls = {"n": 0}
    def fake(*a, **k):
        calls["n"] += 1
        return _R(1, err="boom") if calls["n"] == 1 else _R(0)
    monkeypatch.setattr(subprocess, "run", fake)
    ok, issues = validate.validate("bad", "default")
    assert ok is False
    assert any("schema" in i for i in issues)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_validate.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/tools/validate.py
import subprocess

def validate(manifests: str, namespace: str) -> tuple[bool, list[str]]:
    issues: list[str] = []

    kc = subprocess.run(
        ["kubeconform", "-strict", "-summary", "-"],
        input=manifests, capture_output=True, text=True,
    )
    if kc.returncode != 0:
        issues.append("schema: " + (kc.stdout + kc.stderr).strip())

    dr = subprocess.run(
        ["kubectl", "apply", "--dry-run=server", "-n", namespace, "-f", "-"],
        input=manifests, capture_output=True, text=True,
    )
    if dr.returncode != 0:
        issues.append("dry-run: " + dr.stderr.strip())

    return (len(issues) == 0, issues)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_validate.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/tools/validate.py backend/tests/test_validate.py
git commit -m "feat: validation tool (kubeconform + server dry-run)"
```

---

### Task 6: Deploy tool

**Files:**
- Create: `backend/tools/deploy.py`

**Interfaces:**
- Consumes: chart (Task 3), `cfg`.
- Produces: `install(cfg) -> None`, `get_replicas(name, ns) -> (ready, desired)`, `get_endpoint(name, ns) -> dict`. Used by the coordinator (Task 7).

- [ ] **Step 1: Write `backend/tools/deploy.py`**

```python
# backend/tools/deploy.py
import json
import os
import subprocess
import tempfile
import yaml
from tools.manifests import build_values, CHART_DIR

def install(cfg: dict) -> None:
    values = build_values(cfg)
    ns = cfg.get("namespace", "default")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
        vfile = f.name
    try:
        subprocess.run(
            ["helm", "upgrade", "--install", values["name"], CHART_DIR,
             "-f", vfile, "--namespace", ns, "--create-namespace"],
            capture_output=True, text=True, check=True,
        )
    finally:
        os.unlink(vfile)

def get_replicas(name: str, namespace: str) -> tuple[int, int]:
    out = subprocess.run(
        ["kubectl", "get", "deploy", name, "-n", namespace, "-o", "json"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return (0, 0)
    status = json.loads(out.stdout).get("status", {})
    return (int(status.get("readyReplicas", 0)), int(status.get("replicas", 0)))

def get_endpoint(name: str, namespace: str) -> dict:
    port = build_values({"name": name, "image": "x", **{}}).get("port", 8080)
    return {
        "service": f"{name}.{namespace}.svc.cluster.local",
        "port": port,
        "port_forward": f"kubectl port-forward -n {namespace} svc/{name} {port}:{port}",
    }
```

Note (ponytail): Phase 0 access is via `kubectl port-forward` — the endpoint dict returns the command. Real ingress URL + TLS is Phase 1. `get_endpoint` recomputes the default port; the coordinator passes the real `cfg` port in Task 7, so replace the body's port with `cfg["port"]` there.

- [ ] **Step 2: Adjust `get_endpoint` to take the port explicitly**

Replace `get_endpoint` with:

```python
def get_endpoint(name: str, namespace: str, port: int) -> dict:
    return {
        "service": f"{name}.{namespace}.svc.cluster.local",
        "port": port,
        "port_forward": f"kubectl port-forward -n {namespace} svc/{name} {port}:{port}",
    }
```

- [ ] **Step 3: Sanity-check import**

Run: `cd backend && python -c "from tools import deploy; print('ok')"`
Expected: prints `ok` (no import errors).

- [ ] **Step 4: Commit**

```bash
git add backend/tools/deploy.py
git commit -m "feat: deploy tool (helm install, replica status, endpoint)"
```

Deploy is exercised end-to-end in Task 10 against the real cluster (it needs one; no meaningful unit test beyond import).

---

### Task 7: Coordinator (async FSM, the only emitter)

**Files:**
- Create: `backend/coordinator.py`
- Test: `backend/tests/test_coordinator.py`

**Interfaces:**
- Consumes: `events.EventBus`, tools (`manifests`, `validate`, `deploy`), `cfg`.
- Produces: `async run(cfg, bus) -> None`. Publishes `stage_enter`/`stage_exit`/`command`/`rollout`/`endpoint`/`error` events. Used by `main.py`.

- [ ] **Step 1: Write the failing test (inject fake tools)**

```python
# backend/tests/test_coordinator.py
import asyncio
import pytest
from events import EventBus
import coordinator

@pytest.mark.asyncio
async def test_happy_path_emits_stages_and_endpoint(monkeypatch):
    monkeypatch.setattr(coordinator.manifests, "render", lambda cfg: "kind: Deployment")
    monkeypatch.setattr(coordinator.validate, "validate", lambda m, ns: (True, []))
    monkeypatch.setattr(coordinator.deploy, "install", lambda cfg: None)
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (2, 2))
    monkeypatch.setattr(coordinator.deploy, "get_endpoint",
                        lambda n, ns, p: {"service": "s", "port": p, "port_forward": "pf"})

    bus = EventBus()
    q = bus.subscribe()
    await coordinator.run({"name": "app", "image": "i:1", "namespace": "default",
                           "port": 8080, "replicas": 2}, bus)

    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "stage_enter" in types
    assert "endpoint" in types
    assert "error" not in types

@pytest.mark.asyncio
async def test_validation_failure_stops_before_deploy(monkeypatch):
    monkeypatch.setattr(coordinator.manifests, "render", lambda cfg: "bad")
    monkeypatch.setattr(coordinator.validate, "validate", lambda m, ns: (False, ["schema: nope"]))
    installed = {"called": False}
    monkeypatch.setattr(coordinator.deploy, "install",
                        lambda cfg: installed.__setitem__("called", True))

    bus = EventBus()
    q = bus.subscribe()
    await coordinator.run({"name": "app", "image": "i:1", "namespace": "default",
                           "port": 8080, "replicas": 2}, bus)

    assert installed["called"] is False
    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "error" in types
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_coordinator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'coordinator'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/coordinator.py
import asyncio
from events import Event, EventBus
from tools import manifests, validate, deploy

ROLLOUT_TIMEOUT_S = 120

async def run(cfg: dict, bus: EventBus) -> None:
    name, ns = cfg["name"], cfg.get("namespace", "default")
    port = int(cfg.get("port", 8080))

    async def emit(type_, stage, message, data=None):
        await bus.publish(Event(type=type_, stage=stage, message=message, data=data or {}))

    try:
        # Generate
        await emit("stage_enter", "Generate", "Rendering manifests via Helm")
        rendered = await asyncio.to_thread(manifests.render, cfg)
        await emit("manifest", "Generate", "Rendered manifests", {"yaml": rendered})
        await emit("stage_exit", "Generate", "Manifests ready")

        # Validate
        await emit("stage_enter", "Validate", "Validating manifests")
        ok, issues = await asyncio.to_thread(validate.validate, rendered, ns)
        if not ok:
            await emit("error", "Validate", "Validation failed", {"issues": issues})
            return
        await emit("stage_exit", "Validate", "Validation passed")

        # Deploy
        await emit("stage_enter", "Deploy", "Applying to cluster")
        await emit("command", "Deploy", f"helm upgrade --install {name} chart")
        await asyncio.to_thread(deploy.install, cfg)

        # Verify (rollout watch with timeout)
        await emit("stage_enter", "Verify", "Waiting for rollout")
        last = None
        for _ in range(ROLLOUT_TIMEOUT_S // 2):
            ready, desired = await asyncio.to_thread(deploy.get_replicas, name, ns)
            if (ready, desired) != last:
                await emit("rollout", "Verify", f"{ready}/{desired} ready",
                           {"ready": ready, "desired": desired})
                last = (ready, desired)
            if desired and ready >= desired:
                break
            await asyncio.sleep(2)
        else:
            await emit("error", "Verify", "Rollout did not complete in time",
                       {"timeout_s": ROLLOUT_TIMEOUT_S})
            return

        ep = await asyncio.to_thread(deploy.get_endpoint, name, ns, port)
        await emit("endpoint", "Verify", "Deployment is live", ep)
        await emit("stage_exit", "Verify", "Done")
    except Exception as e:  # surface, never hang
        await emit("error", "Deploy", f"Unexpected error: {e}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_coordinator.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/coordinator.py backend/tests/test_coordinator.py
git commit -m "feat: coordinator FSM emits every step, gates on validation"
```

---

### Task 8: FastAPI app (deploy trigger + SSE stream + UI)

**Files:**
- Create: `backend/main.py`
- Test: `backend/tests/test_main.py`

**Interfaces:**
- Consumes: `coordinator.run`, `EventBus`, `static/index.html`.
- Produces: `POST /deploy`, `GET /events` (SSE), `GET /` (UI). App object `app`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_main.py
from fastapi.testclient import TestClient
import main

def test_deploy_accepts_config_and_returns_id(monkeypatch):
    async def fake_run(cfg, bus):
        return None
    monkeypatch.setattr(main, "coordinator_run", fake_run)
    client = TestClient(main.app)
    r = client.post("/deploy", json={"name": "app", "image": "i:1",
                                     "namespace": "default", "port": 8080, "replicas": 2})
    assert r.status_code == 200
    assert "deployment_id" in r.json()

def test_root_serves_ui():
    client = TestClient(main.app)
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_main.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'main'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/main.py
import asyncio
import json
import os
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from events import EventBus
from coordinator import run as coordinator_run

app = FastAPI(title="Helmsman")
bus = EventBus()
STATIC = os.path.join(os.path.dirname(__file__), "static")

class DeployRequest(BaseModel):
    name: str
    image: str
    namespace: str = "default"
    port: int = 8080
    replicas: int = 2

@app.post("/deploy")
async def deploy(req: DeployRequest):
    asyncio.create_task(coordinator_run(req.model_dump(), bus))
    return {"deployment_id": req.name}

@app.get("/events")
async def events():
    q = bus.subscribe()
    async def gen():
        try:
            while True:
                ev = await q.get()
                yield f"data: {json.dumps(ev.to_dict())}\n\n"
        finally:
            bus.unsubscribe(q)
    return StreamingResponse(gen(), media_type="text/event-stream")

@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC, "index.html"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_main.py -v`
Expected: PASS (2 passed). (The `/` test needs `static/index.html` — create a stub now if Task 9 not done: `mkdir -p backend/static && echo '<!doctype html><title>Helmsman</title>' > backend/static/index.html`.)

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_main.py
git commit -m "feat: FastAPI app with deploy trigger and SSE stream"
```

---

### Task 9: Minimal live UI

**Files:**
- Create: `backend/static/index.html`

**Interfaces:**
- Consumes: `GET /events` (SSE), `POST /deploy`.
- Produces: a browser page that triggers a deploy and renders every event live.

- [ ] **Step 1: Write `backend/static/index.html`**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Helmsman</title>
<style>
  body { font-family: ui-monospace, Menlo, monospace; background:#0d1319; color:#e9eff3; margin:0; padding:24px; }
  h1 { font-size:1.1rem; color:#2dd4bf; }
  form { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:16px; }
  input { background:#141d26; border:1px solid #25323d; color:#e9eff3; padding:6px 8px; border-radius:6px; }
  button { background:#2dd4bf; color:#0d1319; border:0; padding:6px 14px; border-radius:6px; font-weight:700; cursor:pointer; }
  #log { background:#141d26; border:1px solid #25323d; border-radius:10px; padding:12px; height:60vh; overflow:auto; }
  .ev { padding:3px 0; border-bottom:1px solid #1b2731; font-size:.85rem; }
  .stage { color:#60a5fa; } .error { color:#f87171; } .endpoint { color:#34d399; }
  .t { color:#647585; }
</style>
</head>
<body>
  <h1>⎈ Helmsman — live deployment</h1>
  <form id="f">
    <input name="name" placeholder="name" value="demo" required>
    <input name="image" placeholder="image" value="mendhak/http-https-echo:31" required>
    <input name="namespace" placeholder="namespace" value="default">
    <input name="port" placeholder="port" value="8080" type="number">
    <input name="replicas" placeholder="replicas" value="2" type="number">
    <button type="submit">Deploy</button>
  </form>
  <div id="log"></div>
<script>
  const log = document.getElementById("log");
  const es = new EventSource("/events");
  es.onmessage = (m) => {
    const e = JSON.parse(m.data);
    const div = document.createElement("div");
    div.className = "ev " + (e.type === "error" ? "error" : e.type === "endpoint" ? "endpoint" : e.type.startsWith("stage") ? "stage" : "");
    const time = new Date(e.ts * 1000).toLocaleTimeString();
    let extra = "";
    if (e.type === "endpoint") extra = " → " + e.data.port_forward;
    if (e.type === "error" && e.data.issues) extra = " → " + e.data.issues.join("; ");
    div.innerHTML = `<span class="t">${time}</span> [${e.stage}] ${e.message}${extra}`;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  };
  document.getElementById("f").onsubmit = async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    await fetch("/deploy", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        name: fd.get("name"), image: fd.get("image"), namespace: fd.get("namespace"),
        port: Number(fd.get("port")), replicas: Number(fd.get("replicas")),
      }),
    });
  };
</script>
</body>
</html>
```

- [ ] **Step 2: Verify the full test suite still passes**

Run: `cd backend && python -m pytest -v`
Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add backend/static/index.html
git commit -m "feat: minimal live UI subscribing to SSE"
```

---

### Task 10: End-to-end verification (definition of done)

**Files:** none (verification only).

**Interfaces:**
- Consumes: everything above + the running `kind` cluster.

- [ ] **Step 1: Ensure cluster is up**

Run: `bash scripts/kind-up.sh && kubectl config use-context kind-helmsman`
Expected: context set to `kind-helmsman`.

- [ ] **Step 2: Start the backend**

Run: `bash scripts/dev.sh` (leave running in one terminal)
Expected: uvicorn serves on `http://localhost:8000`.

- [ ] **Step 3: Deploy the sample app via the UI**

Open `http://localhost:8000`, keep defaults (image `mendhak/http-https-echo:31`, port 8080), click **Deploy**.
Expected in the live log, in order: `[Generate] Rendered manifests` → `[Validate] Validation passed` → `[Deploy] helm upgrade --install` → `[Verify] 0/2 ready` → `[Verify] 2/2 ready` → `[Verify] Deployment is live → kubectl port-forward ...`.

- [ ] **Step 4: Confirm the app actually serves**

Run (new terminal): the `kubectl port-forward` command shown in the endpoint event, then `curl -s localhost:8080 | head`
Expected: an HTTP/JSON response from the echo server — proves the deployed app is reachable and healthy.

- [ ] **Step 5: Confirm the cluster state**

Run: `kubectl get deploy,pods,svc -l app.kubernetes.io/name=demo`
Expected: deployment `demo` with 2/2 ready pods and a ClusterIP service.
Note: filter by `app.kubernetes.io/name`, NOT `managed-by=helmsman` — Helm overrides
`app.kubernetes.io/managed-by` to `Helm` on install regardless of the chart value. A
custom, non-colliding label key is a Phase 1/2 fix (needed before monitoring uses label selection).

- [ ] **Step 6: Negative check — validation gate**

Deploy again with an obviously invalid **port** (e.g. `port: 99999`, out of the valid 1–65535 range) via the UI.
Expected: pipeline stops at `[Validate]` with an `error` event; `helm upgrade` is NOT run and no
deployment is created. (Server-side dry-run rejects the out-of-range port before any apply.)
Note: an invalid *image tag* is NOT caught here — neither kubeconform nor server dry-run pulls the
image; a bad image surfaces later as ImagePullBackOff → the Verify rollout timeout. Use an invalid
port (or negative replicas) to exercise the validation gate specifically.

- [ ] **Step 7: Final commit**

```bash
git add -A
git commit -m "test: phase 0 walking skeleton verified end-to-end on kind"
```

---

## Self-review notes

- **Spec coverage:** Generate/Validate/Deploy/Verify stages (§4) ✔; Helm one-chart + values (§4/§10) ✔; kubeconform + server dry-run (§4 step 5) ✔; readiness-gated success (§4 step 8) ✔; §6 defaults in chart ✔; rollout timeout, never hang (§13) ✔; event bus + SSE, UI is pure subscriber (§3.1) ✔; no LLM (Phase 0) ✔; plain FSM, no LangGraph ✔.
- **Deferred to later phases (by design):** secret redaction + ConfigMap/Secret/Ingress/HPA/PDB (Phase 1); monitoring (Phase 2); LLM agents (Phase 3). Endpoint access via `port-forward` in Phase 0; real ingress URL in Phase 1.
- **Type consistency:** `get_endpoint(name, ns, port)` signature is used identically in `deploy.py`, the coordinator, and its test. `EventBus.subscribe/unsubscribe/publish` and `Event` fields match across events/coordinator/main.
