# Phase 1 — Full Manifests + Approval Gates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Extend the walking skeleton to the full manifest set (ConfigMap, Secret, Ingress, HPA, PDB) with mandatory secret redaction, a manual approval gate, capability detection (auto-skip Ingress/HPA when the cluster can't support them), kube-score validation, plus the two Phase-0 review fixes (RFC1123 input validation, non-colliding `managed-by` label).

**Architecture:** The coordinator gains an **Approve** stage between Validate and Deploy; in Manual mode it emits `approval_required` and awaits an approval Future resolved by `POST /approve`. Capability detection runs before Generate and the coordinator mutates `cfg` (disabling Ingress/HPA the cluster can't serve) then renders. All event emission passes through a single redaction choke point in the coordinator's `emit`, so registered secret values can never reach the store/UI. Tools stay pure `build_values(cfg)`.

**Tech Stack:** Python 3.12, FastAPI, PyYAML, pytest; Helm, kubeconform, kube-score, kubectl, kind.

## Global Constraints

- **Secret redaction is mandatory.** Secrets render as `••••` in the event stream; raw values never enter the event store, logs, or rendered commands. (Spec §7.1)
- **Autonomous ≠ auto-destructive; Manual gates each mutating action.** (Spec §5)
- **Validate before touching the cluster:** kubeconform + `kubectl apply --dry-run=server` + **kube-score** gate every deploy. (Spec §4 step 5)
- **Generated manifests carry best-practice defaults**; PDB/HPA/Ingress/NetworkPolicy where applicable and **detected — never assumed**. (Spec §6, §13)
- **Deterministic core, NO LLM in Phase 1.** (LLM is Phase 3.)
- **Cluster text is untrusted** — no change here but keep it in mind.
- Commits: author is the repo git identity (`mittal122`), **NO `Co-Authored-By` trailer**. Do not add any Claude/Anthropic attribution to commit messages. The controller pushes after each task.

## Interfaces locked across tasks

- `manifests.build_values(cfg: dict) -> dict` — now emits nested `env`, `secrets`, `ingress{enabled,host}`, `hpa{enabled,minReplicas,maxReplicas,targetCPU}`, `pdb{enabled,minAvailable}`, `resources` in addition to `name/image/port/replicas/probePath`.
- `guardrails.secret_variants(secrets: dict) -> set[str]` — raw values + their base64 encodings (non-empty only).
- `guardrails.redact(obj, variants: set[str]) -> obj` — deep copy with every `variant` substring in any string replaced by `••••`.
- `deploy.detect_capabilities() -> dict` — `{"ingress_controller": bool, "metrics_server": bool}`.
- `approvals.Approvals` — `.create(key) -> asyncio.Future`, `.resolve(key, approved: bool) -> bool`.
- `coordinator.run(cfg: dict, bus: EventBus, approvals: Approvals) -> None` — Approve stage added; emit redacts.
- `validate.validate(manifests: str, namespace: str) -> tuple[bool, list[str]]` — now also runs kube-score (CRITICAL findings block).

`cfg` shape (Phase 1): `{name, image, namespace, port, replicas, mode, env:dict, secrets:dict, ingress_host:str, hpa_enabled:bool, hpa_min:int, hpa_max:int, hpa_cpu:int}`.

---

### Task 1: kube-score in validation

**Files:** Modify `backend/tools/validate.py`; Modify `backend/tests/test_validate.py`

**Interfaces:** Produces the same `validate(manifests, namespace) -> (ok, issues)` but adds a third check.

- [ ] **Step 1: Install kube-score**

```bash
curl -fsSL https://github.com/zegl/kube-score/releases/latest/download/kube-score_linux_amd64.tar.gz \
  | tar xz -C /tmp kube-score && mv /tmp/kube-score ~/.local/bin/ && kube-score version
```

- [ ] **Step 2: Write the failing test** (append to `test_validate.py`)

```python
def test_kube_score_critical_blocks(monkeypatch):
    seq = [_R(0), _R(0), _R(0, out="[CRITICAL] Deployment/x: something bad")]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seq.pop(0))
    ok, issues = validate.validate("kind: Deployment", "default")
    assert ok is False
    assert any("kube-score" in i for i in issues)

def test_kube_score_warnings_pass(monkeypatch):
    seq = [_R(0), _R(0), _R(0, out="[WARNING] minor")]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seq.pop(0))
    ok, issues = validate.validate("kind: Deployment", "default")
    assert ok is True
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_validate.py -k kube_score -v`
Expected: FAIL (kube-score branch not present).

- [ ] **Step 4: Add kube-score to `validate.py`** (after the dry-run block, before `return`)

```python
    ks = subprocess.run(
        ["kube-score", "score", "--output-format", "ci", "-"],
        input=manifests, capture_output=True, text=True,
    )
    criticals = [ln for ln in ks.stdout.splitlines() if "[CRITICAL]" in ln]
    if criticals:
        issues.append("kube-score: " + "; ".join(criticals))
```

- [ ] **Step 5: Run tests to verify pass**

Run: `cd backend && python -m pytest tests/test_validate.py -v`
Expected: PASS (all validate tests).

- [ ] **Step 6: Commit**

```bash
git add backend/tools/validate.py backend/tests/test_validate.py
git commit -m "feat: add kube-score critical-findings gate to validation"
```

---

### Task 2: Chart — managed-by label fix + ConfigMap + Secret + Deployment envFrom

**Files:** Modify `chart/templates/_helpers.tpl`, `chart/values.yaml`, `chart/templates/deployment.yaml`; Create `chart/templates/configmap.yaml`, `chart/templates/secret.yaml`

**Interfaces:** Chart renders ConfigMap (when `.Values.env`) and Secret (when `.Values.secrets`), and the Deployment consumes both via `envFrom`. Deployment/Service now labelled with a non-colliding managed-by key.

- [ ] **Step 1: Fix the managed-by label** in `chart/templates/_helpers.tpl`

```
{{- define "app.labels" -}}
app.kubernetes.io/name: {{ .Values.name }}
helmsman.dev/managed-by: helmsman
{{- end -}}
```

- [ ] **Step 2: Add defaults to `chart/values.yaml`** (append)

```yaml
env: {}
secrets: {}
ingress:
  enabled: false
  host: ""
hpa:
  enabled: false
  minReplicas: 2
  maxReplicas: 5
  targetCPU: 80
pdb:
  enabled: false
  minAvailable: 1
```

- [ ] **Step 3: Create `chart/templates/configmap.yaml`**

```yaml
{{- if .Values.env }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Values.name }}-config
  labels:
    {{- include "app.labels" . | nindent 4 }}
data:
{{- range $k, $v := .Values.env }}
  {{ $k }}: {{ $v | quote }}
{{- end }}
{{- end }}
```

- [ ] **Step 4: Create `chart/templates/secret.yaml`** (stringData — plaintext at render time, redacted before it ever reaches the UI)

```yaml
{{- if .Values.secrets }}
apiVersion: v1
kind: Secret
metadata:
  name: {{ .Values.name }}-secret
  labels:
    {{- include "app.labels" . | nindent 4 }}
type: Opaque
stringData:
{{- range $k, $v := .Values.secrets }}
  {{ $k }}: {{ $v | quote }}
{{- end }}
{{- end }}
```

- [ ] **Step 5: Wire `envFrom` into `chart/templates/deployment.yaml`** — add immediately after the container's `ports:` block, at the same indentation as `ports`:

```yaml
          envFrom:
{{- if .Values.env }}
            - configMapRef:
                name: {{ .Values.name }}-config
{{- end }}
{{- if .Values.secrets }}
            - secretRef:
                name: {{ .Values.name }}-secret
{{- end }}
```

- [ ] **Step 6: Verify render + schema (with sample env/secrets)**

Run:
```bash
helm template app chart --set env.FOO=bar --set secrets.TOKEN=s3cret | kubeconform -strict -summary -
helm template app chart --set env.FOO=bar --set secrets.TOKEN=s3cret | grep -E 'kind: (ConfigMap|Secret)'
```
Expected: kubeconform 0 errors; both `kind: ConfigMap` and `kind: Secret` appear. Also confirm the default render (no env/secrets) still shows no ConfigMap/Secret: `helm template app chart | grep -c 'kind: ConfigMap'` → `0`.

- [ ] **Step 7: Commit**

```bash
git add chart/
git commit -m "feat: chart ConfigMap+Secret, envFrom wiring, non-colliding managed-by label"
```

---

### Task 3: Chart — Ingress + HPA + PDB (value-gated)

**Files:** Create `chart/templates/ingress.yaml`, `chart/templates/hpa.yaml`, `chart/templates/pdb.yaml`

**Interfaces:** Each renders only when its `.Values.<x>.enabled` is true.

- [ ] **Step 1: Create `chart/templates/ingress.yaml`**

```yaml
{{- if .Values.ingress.enabled }}
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ .Values.name }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
spec:
  rules:
    - host: {{ .Values.ingress.host | quote }}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ .Values.name }}
                port:
                  number: {{ .Values.port }}
{{- end }}
```

- [ ] **Step 2: Create `chart/templates/hpa.yaml`**

```yaml
{{- if .Values.hpa.enabled }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ .Values.name }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ .Values.name }}
  minReplicas: {{ .Values.hpa.minReplicas }}
  maxReplicas: {{ .Values.hpa.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .Values.hpa.targetCPU }}
{{- end }}
```

- [ ] **Step 3: Create `chart/templates/pdb.yaml`**

```yaml
{{- if .Values.pdb.enabled }}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ .Values.name }}
  labels:
    {{- include "app.labels" . | nindent 4 }}
spec:
  minAvailable: {{ .Values.pdb.minAvailable }}
  selector:
    matchLabels:
      app.kubernetes.io/name: {{ .Values.name }}
{{- end }}
```

- [ ] **Step 4: Verify both enabled and disabled render cleanly**

Run:
```bash
helm template app chart --set ingress.enabled=true --set ingress.host=demo.local \
  --set hpa.enabled=true --set pdb.enabled=true | kubeconform -strict -summary -
helm template app chart --set ingress.enabled=true --set ingress.host=demo.local \
  --set hpa.enabled=true --set pdb.enabled=true | grep -E 'kind: (Ingress|HorizontalPodAutoscaler|PodDisruptionBudget)'
helm template app chart | grep -cE 'kind: (Ingress|HorizontalPodAutoscaler|PodDisruptionBudget)'
```
Expected: kubeconform 0 errors; all three kinds appear when enabled; the last command prints `0` (none render by default).

- [ ] **Step 5: Commit**

```bash
git add chart/
git commit -m "feat: chart Ingress, HPA, PDB templates gated by values"
```

---

### Task 4: manifests.build_values extension

**Files:** Modify `backend/tools/manifests.py`; Modify `backend/tests/test_manifests.py`

**Interfaces:** `build_values(cfg)` now emits the full nested values.

- [ ] **Step 1: Write the failing tests** (append to `test_manifests.py`)

```python
def test_build_values_env_secrets_pdb():
    v = manifests.build_values({
        "name": "x", "image": "x:1", "replicas": 3,
        "env": {"A": "1"}, "secrets": {"S": "y"},
    })
    assert v["env"] == {"A": "1"}
    assert v["secrets"] == {"S": "y"}
    assert v["pdb"]["enabled"] is True          # replicas > 1
    assert v["pdb"]["minAvailable"] == 1

def test_build_values_ingress_hpa_flags():
    v = manifests.build_values({
        "name": "x", "image": "x:1", "replicas": 1,
        "ingress_host": "demo.local",
        "hpa_enabled": True, "hpa_min": 2, "hpa_max": 6, "hpa_cpu": 70,
    })
    assert v["ingress"] == {"enabled": True, "host": "demo.local"}
    assert v["hpa"]["enabled"] is True and v["hpa"]["maxReplicas"] == 6
    assert v["pdb"]["enabled"] is False         # single replica
```

- [ ] **Step 2: Run to verify fail**

Run: `cd backend && python -m pytest tests/test_manifests.py -k "env_secrets or ingress_hpa" -v`
Expected: FAIL (KeyError).

- [ ] **Step 3: Replace `build_values` in `manifests.py`**

```python
def build_values(cfg: dict) -> dict:
    replicas = int(cfg.get("replicas", 2))
    return {
        "name": cfg["name"],
        "image": cfg["image"],
        "port": int(cfg.get("port", 8080)),
        "replicas": replicas,
        "env": dict(cfg.get("env") or {}),
        "secrets": dict(cfg.get("secrets") or {}),
        "ingress": {
            "enabled": bool(cfg.get("ingress_host")),
            "host": cfg.get("ingress_host") or "",
        },
        "hpa": {
            "enabled": bool(cfg.get("hpa_enabled")),
            "minReplicas": int(cfg.get("hpa_min", 2)),
            "maxReplicas": int(cfg.get("hpa_max", 5)),
            "targetCPU": int(cfg.get("hpa_cpu", 80)),
        },
        "pdb": {"enabled": replicas > 1, "minAvailable": 1},
    }
```

- [ ] **Step 4: Run to verify pass**

Run: `cd backend && python -m pytest tests/test_manifests.py -v`
Expected: PASS (existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add backend/tools/manifests.py backend/tests/test_manifests.py
git commit -m "feat: build_values emits env/secrets/ingress/hpa/pdb"
```

---

### Task 5: guardrails.py — secret redaction

**Files:** Create `backend/guardrails.py`, `backend/tests/test_guardrails.py`

**Interfaces:** `secret_variants(secrets) -> set[str]`, `redact(obj, variants) -> obj`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_guardrails.py
import base64
import guardrails

def test_variants_include_raw_and_base64():
    v = guardrails.secret_variants({"T": "s3cret", "EMPTY": ""})
    assert "s3cret" in v
    assert base64.b64encode(b"s3cret").decode() in v
    assert "" not in v          # empty values are not redacted

def test_redact_replaces_in_nested_structures():
    variants = guardrails.secret_variants({"T": "s3cret"})
    obj = {"msg": "token is s3cret here", "list": ["x s3cret", "clean"]}
    out = guardrails.redact(obj, variants)
    assert "s3cret" not in str(out)
    assert "••••" in out["msg"]
    assert out["list"][1] == "clean"
    # original unchanged (deep copy)
    assert obj["msg"] == "token is s3cret here"

def test_redact_noop_without_variants():
    assert guardrails.redact({"a": "b"}, set()) == {"a": "b"}
```

- [ ] **Step 2: Run to verify fail**

Run: `cd backend && python -m pytest tests/test_guardrails.py -v`
Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Write `backend/guardrails.py`**

```python
import base64
import copy

REDACTED = "••••"

def secret_variants(secrets: dict) -> set[str]:
    out: set[str] = set()
    for v in (secrets or {}).values():
        if not v:
            continue
        s = str(v)
        out.add(s)
        out.add(base64.b64encode(s.encode()).decode())
    return out

def _redact_str(s: str, variants: set[str]) -> str:
    for v in variants:
        if v and v in s:
            s = s.replace(v, REDACTED)
    return s

def redact(obj, variants: set[str]):
    if not variants:
        return obj
    obj = copy.deepcopy(obj)

    def walk(x):
        if isinstance(x, str):
            return _redact_str(x, variants)
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x

    return walk(obj)
```

- [ ] **Step 4: Run to verify pass**

Run: `cd backend && python -m pytest tests/test_guardrails.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/guardrails.py backend/tests/test_guardrails.py
git commit -m "feat: guardrails secret redaction (raw + base64, deep copy)"
```

---

### Task 6: Capability detection

**Files:** Modify `backend/tools/deploy.py`

**Interfaces:** `detect_capabilities() -> {"ingress_controller": bool, "metrics_server": bool}`.

- [ ] **Step 1: Add to `deploy.py`**

```python
def detect_capabilities() -> dict:
    ic = subprocess.run(["kubectl", "get", "ingressclass", "-o", "name"],
                        capture_output=True, text=True)
    ms = subprocess.run(["kubectl", "get", "apiservices", "v1beta1.metrics.k8s.io"],
                        capture_output=True, text=True)
    return {
        "ingress_controller": ic.returncode == 0 and bool(ic.stdout.strip()),
        "metrics_server": ms.returncode == 0,
    }
```

- [ ] **Step 2: Sanity import check**

Run: `cd backend && python -c "from tools import deploy; print(deploy.detect_capabilities.__name__)"`
Expected: prints `detect_capabilities` (no import error).

- [ ] **Step 3: Commit**

```bash
git add backend/tools/deploy.py
git commit -m "feat: cluster capability detection (ingress controller, metrics-server)"
```

Real detection is exercised in Task 10 against kind.

---

### Task 7: approvals.py + coordinator Approve stage, redaction, capability gating

**Files:** Create `backend/approvals.py`; Modify `backend/coordinator.py`; Modify `backend/tests/test_coordinator.py`; Create `backend/tests/test_approvals.py`

**Interfaces:** `Approvals.create(key)/resolve(key, approved)`; `coordinator.run(cfg, bus, approvals)`.

- [ ] **Step 1: Write `backend/approvals.py`**

```python
import asyncio

class Approvals:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future] = {}

    def create(self, key: str) -> asyncio.Future:
        fut = asyncio.get_event_loop().create_future()
        self._pending[key] = fut
        return fut

    def resolve(self, key: str, approved: bool) -> bool:
        fut = self._pending.pop(key, None)
        if fut and not fut.done():
            fut.set_result(approved)
            return True
        return False
```

- [ ] **Step 2: Write `backend/tests/test_approvals.py`**

```python
import asyncio
import pytest
from approvals import Approvals

@pytest.mark.asyncio
async def test_resolve_completes_future():
    a = Approvals()
    fut = a.create("d1")
    assert a.resolve("d1", True) is True
    assert await fut is True

@pytest.mark.asyncio
async def test_resolve_unknown_key_returns_false():
    a = Approvals()
    assert a.resolve("nope", True) is False
```

- [ ] **Step 3: Write failing coordinator tests** (append to `test_coordinator.py`)

```python
import approvals as approvals_mod

def _stub_tools(monkeypatch):
    monkeypatch.setattr(coordinator.manifests, "render", lambda cfg: "kind: Deployment")
    monkeypatch.setattr(coordinator.validate, "validate", lambda m, ns: (True, []))
    monkeypatch.setattr(coordinator.deploy, "detect_capabilities",
                        lambda: {"ingress_controller": True, "metrics_server": True})
    monkeypatch.setattr(coordinator.deploy, "install", lambda cfg: None)
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (1, 1))
    monkeypatch.setattr(coordinator.deploy, "get_endpoint",
                        lambda n, ns, p: {"service": "s", "port": p, "port_forward": "pf"})

def _cfg(**over):
    base = {"name": "app", "image": "i:1", "namespace": "default", "port": 8080,
            "replicas": 1, "mode": "manual", "secrets": {}}
    base.update(over); return base

@pytest.mark.asyncio
async def test_manual_mode_waits_for_approval_then_deploys(monkeypatch):
    _stub_tools(monkeypatch)
    installed = {"called": False}
    monkeypatch.setattr(coordinator.deploy, "install",
                        lambda cfg: installed.__setitem__("called", True))
    bus = EventBus(); q = bus.subscribe(); appr = approvals_mod.Approvals()
    task = asyncio.create_task(coordinator.run(_cfg(), bus, appr))
    await asyncio.sleep(0.05)
    assert installed["called"] is False           # blocked pending approval
    assert appr.resolve("app", True) is True       # approve
    await task
    assert installed["called"] is True

@pytest.mark.asyncio
async def test_manual_reject_stops_before_deploy(monkeypatch):
    _stub_tools(monkeypatch)
    installed = {"called": False}
    monkeypatch.setattr(coordinator.deploy, "install",
                        lambda cfg: installed.__setitem__("called", True))
    bus = EventBus(); q = bus.subscribe(); appr = approvals_mod.Approvals()
    task = asyncio.create_task(coordinator.run(_cfg(), bus, appr))
    await asyncio.sleep(0.05)
    appr.resolve("app", False)
    await task
    assert installed["called"] is False
    types = [ (await q.get()).type for _ in range(q.qsize()) ]
    assert "rejected" in types

@pytest.mark.asyncio
async def test_autonomous_mode_skips_gate(monkeypatch):
    _stub_tools(monkeypatch)
    bus = EventBus(); q = bus.subscribe(); appr = approvals_mod.Approvals()
    await coordinator.run(_cfg(mode="autonomous"), bus, appr)
    types = [ (await q.get()).type for _ in range(q.qsize()) ]
    assert "endpoint" in types and "approval_required" not in types

@pytest.mark.asyncio
async def test_secret_values_are_redacted_in_events(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.manifests, "render",
                        lambda cfg: "stringData:\n  TOKEN: s3cret")
    bus = EventBus(); q = bus.subscribe(); appr = approvals_mod.Approvals()
    await coordinator.run(_cfg(mode="autonomous", secrets={"TOKEN": "s3cret"}), bus, appr)
    dumped = ""
    while not q.empty():
        dumped += str((await q.get()).to_dict())
    assert "s3cret" not in dumped
    assert "••••" in dumped
```

- [ ] **Step 4: Run to verify fail**

Run: `cd backend && python -m pytest tests/test_coordinator.py -k "manual or autonomous or redacted" tests/test_approvals.py -v`
Expected: FAIL.

- [ ] **Step 5: Rewrite `backend/coordinator.py`**

```python
import asyncio
from events import Event, EventBus
from tools import manifests, validate, deploy
from approvals import Approvals
import guardrails

ROLLOUT_TIMEOUT_S = 120
POLL_INTERVAL_S = 2

async def run(cfg: dict, bus: EventBus, approvals: Approvals) -> None:
    name, ns = cfg["name"], cfg.get("namespace", "default")
    port = int(cfg.get("port", 8080))
    mode = cfg.get("mode", "manual")
    variants = guardrails.secret_variants(cfg.get("secrets") or {})
    current = "Detect"

    async def emit(type_, stage, message, data=None):
        ev = Event(type=type_, stage=stage,
                   message=guardrails.redact(message, variants),
                   data=guardrails.redact(data or {}, variants))
        await bus.publish(ev)

    try:
        # Detect capabilities and disable what the cluster can't serve
        current = "Detect"
        await emit("stage_enter", "Detect", "Checking cluster capabilities")
        caps = await asyncio.to_thread(deploy.detect_capabilities)
        if cfg.get("ingress_host") and not caps["ingress_controller"]:
            await emit("info", "Detect",
                       "No ingress controller — skipping Ingress, use port-forward")
            cfg["ingress_host"] = ""
        if cfg.get("hpa_enabled") and not caps["metrics_server"]:
            await emit("info", "Detect", "No metrics-server — skipping HPA")
            cfg["hpa_enabled"] = False
        await emit("stage_exit", "Detect", "Capabilities resolved")

        # Generate
        current = "Generate"
        await emit("stage_enter", "Generate", "Rendering manifests via Helm")
        rendered = await asyncio.to_thread(manifests.render, cfg)
        await emit("manifest", "Generate", "Rendered manifests", {"yaml": rendered})
        await emit("stage_exit", "Generate", "Manifests ready")

        # Validate
        current = "Validate"
        await emit("stage_enter", "Validate", "Validating manifests")
        ok, issues = await asyncio.to_thread(validate.validate, rendered, ns)
        if not ok:
            await emit("error", "Validate", "Validation failed", {"issues": issues})
            return
        await emit("stage_exit", "Validate", "Validation passed")

        # Approve
        current = "Approve"
        await emit("stage_enter", "Approve", "Approval stage")
        if mode == "manual":
            await emit("approval_required", "Approve",
                       f"Approve deployment of {name} to {ns}?",
                       {"name": name, "namespace": ns})
            approved = await approvals.create(name)
            if not approved:
                await emit("rejected", "Approve", "Deployment rejected by user")
                return
            await emit("stage_exit", "Approve", "Approved")
        else:
            await emit("info", "Approve", "Autonomous mode — auto-approved")
            await emit("stage_exit", "Approve", "Approved")

        # Deploy
        current = "Deploy"
        await emit("stage_enter", "Deploy", "Applying to cluster")
        await emit("command", "Deploy", f"helm upgrade --install {name} chart")
        await asyncio.to_thread(deploy.install, cfg)
        await emit("stage_exit", "Deploy", "Applied to cluster")

        # Verify
        current = "Verify"
        await emit("stage_enter", "Verify", "Waiting for rollout")
        last = None
        for _ in range(ROLLOUT_TIMEOUT_S // POLL_INTERVAL_S):
            ready, desired = await asyncio.to_thread(deploy.get_replicas, name, ns)
            if (ready, desired) != last:
                await emit("rollout", "Verify", f"{ready}/{desired} ready",
                           {"ready": ready, "desired": desired})
                last = (ready, desired)
            if desired and ready >= desired:
                break
            await asyncio.sleep(POLL_INTERVAL_S)
        else:
            await emit("error", "Verify", "Rollout did not complete in time",
                       {"timeout_s": ROLLOUT_TIMEOUT_S})
            return

        ep = await asyncio.to_thread(deploy.get_endpoint, name, ns, port)
        await emit("endpoint", "Verify", "Deployment is live", ep)
        await emit("stage_exit", "Verify", "Done")
    except Exception as e:
        await emit("error", current, f"Unexpected error: {e}")
```

- [ ] **Step 6: Run to verify pass**

Run: `cd backend && python -m pytest -v`
Expected: all pass (Phase 0 coordinator tests + the 4 new + approvals). If an old Phase-0 coordinator test calls `run(cfg, bus)` without `approvals`, update it to pass `approvals_mod.Approvals()`.

- [ ] **Step 7: Commit**

```bash
git add backend/approvals.py backend/coordinator.py backend/tests/test_coordinator.py backend/tests/test_approvals.py
git commit -m "feat: approval gate, autonomous mode, secret redaction, capability gating in coordinator"
```

---

### Task 8: FastAPI — extended request, RFC1123 validation, /approve

**Files:** Modify `backend/main.py`; Modify `backend/tests/test_main.py`

**Interfaces:** `DeployRequest` gains `mode/env/secrets/ingress_host/hpa_*`; new `POST /approve`; RFC1123 validators on `name`/`namespace`.

- [ ] **Step 1: Write failing tests** (append to `test_main.py`)

```python
def test_rejects_non_rfc1123_name():
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    r = client.post("/deploy", json={"name": "--evil", "image": "i:1"})
    assert r.status_code == 422

def test_approve_resolves(monkeypatch):
    from fastapi.testclient import TestClient
    called = {}
    monkeypatch.setattr(main.approvals, "resolve",
                        lambda k, a: called.update(k=k, a=a) or True)
    client = TestClient(main.app)
    r = client.post("/approve", json={"name": "demo", "approved": True})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert called == {"k": "demo", "a": True}
```

- [ ] **Step 2: Run to verify fail**

Run: `cd backend && python -m pytest tests/test_main.py -k "rfc1123 or approve" -v`
Expected: FAIL.

- [ ] **Step 3: Update `backend/main.py`** — replace the model + deploy handler and add the approve route + approvals instance:

```python
import re
from approvals import Approvals

approvals = Approvals()
_RFC1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

def _dns1123(v: str) -> str:
    if not _RFC1123.match(v) or len(v) > 63:
        raise ValueError("must be a valid RFC1123 name (lowercase alphanumeric/-, no leading -)")
    return v

class DeployRequest(BaseModel):
    name: str
    image: str
    namespace: str = "default"
    port: int = 8080
    replicas: int = 2
    mode: str = "manual"
    env: dict[str, str] = {}
    secrets: dict[str, str] = {}
    ingress_host: str = ""
    hpa_enabled: bool = False
    hpa_min: int = 2
    hpa_max: int = 5
    hpa_cpu: int = 80

    @field_validator("name", "namespace")
    @classmethod
    def _valid_name(cls, v): return _dns1123(v)

    @field_validator("image")
    @classmethod
    def _valid_image(cls, v):
        if v.startswith("-") or any(c.isspace() for c in v):
            raise ValueError("invalid image reference")
        return v

class ApproveRequest(BaseModel):
    name: str
    approved: bool = True

@app.post("/deploy")
async def deploy(req: DeployRequest):
    task = asyncio.create_task(coordinator_run(req.model_dump(), bus, approvals))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"deployment_id": req.name}

@app.post("/approve")
async def approve(req: ApproveRequest):
    return {"ok": approvals.resolve(req.name, req.approved)}
```

Also add `field_validator` to the pydantic import: `from pydantic import BaseModel, field_validator`, and pass `approvals` in the `coordinator_run(...)` call (done above).

- [ ] **Step 4: Run to verify pass**

Run: `cd backend && python -m pytest -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_main.py
git commit -m "feat: extended deploy request, RFC1123 validation, /approve endpoint"
```

---

### Task 9: UI — mode, env/secrets, approval controls

**Files:** Modify `backend/static/index.html`

**Interfaces:** Consumes new event types (`approval_required`, `rejected`, `info`) and `POST /approve`.

- [ ] **Step 1: Extend the form and script** — add to the form (after replicas):

```html
    <select name="mode"><option value="manual">Manual</option><option value="autonomous">Autonomous</option></select>
    <input name="env" placeholder="env KEY=VAL,KEY2=VAL2">
    <input name="secrets" placeholder="secrets KEY=VAL (redacted)">
```

Add an approval bar element after the form:

```html
  <div id="approve" style="display:none;margin:10px 0;padding:10px;border:1px solid #2dd4bf;border-radius:8px">
    <span id="approve-msg"></span>
    <button id="btn-approve" style="background:#2dd4bf">Approve</button>
    <button id="btn-reject" style="background:#f87171">Reject</button>
  </div>
```

In the script, parse the KEY=VAL inputs, send the richer body, handle approval events, and wire the buttons. Add this — keep the existing safe `textContent` render for the log:

```html
<script>
  function kv(s){ const o={}; (s||"").split(",").map(x=>x.trim()).filter(Boolean).forEach(p=>{const i=p.indexOf("=");if(i>0)o[p.slice(0,i)]=p.slice(i+1);}); return o; }
  let currentName = "";
  const approveBar = document.getElementById("approve");
  document.getElementById("btn-approve").onclick = () => fetch("/approve",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:currentName,approved:true})}).then(()=>approveBar.style.display="none");
  document.getElementById("btn-reject").onclick = () => fetch("/approve",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:currentName,approved:false})}).then(()=>approveBar.style.display="none");
</script>
```

Update the existing submit handler to build the body with the new fields, and set `currentName`:

```html
<script>
  document.getElementById("f").onsubmit = async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    currentName = fd.get("name");
    await fetch("/deploy", { method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        name: fd.get("name"), image: fd.get("image"), namespace: fd.get("namespace"),
        port: Number(fd.get("port")), replicas: Number(fd.get("replicas")),
        mode: fd.get("mode"), env: kv(fd.get("env")), secrets: kv(fd.get("secrets")),
      })});
  };
</script>
```

In the SSE `onmessage`, after appending the log row, show the approval bar on the right event:

```javascript
    if (e.type === "approval_required") {
      document.getElementById("approve-msg").textContent = e.message + " ";
      approveBar.style.display = "block";
    }
```

Note: keep exactly ONE submit handler — replace the Phase-0 one, don't add a second.

- [ ] **Step 2: Verify tests still green**

Run: `cd backend && python -m pytest -q`
Expected: all pass. Also `grep -c innerHTML backend/static/index.html` → `0` (never reintroduce the XSS sink).

- [ ] **Step 3: Commit**

```bash
git add backend/static/index.html
git commit -m "feat: UI mode selector, env/secret inputs, approval controls"
```

---

### Task 10: End-to-end verification (definition of done)

**Files:** none.

- [ ] **Step 1: Cluster up + backend running**

```bash
bash scripts/kind-up.sh && kubectl config use-context kind-helmsman
bash scripts/dev.sh   # in one terminal
```

- [ ] **Step 2: Manual deploy with env + secret**

Open `http://localhost:8000`. Fill: name `demo`, image `mendhak/http-https-echo:31`, port 8080, replicas 2, mode `manual`, env `GREETING=hi`, secrets `TOKEN=s3cretvalue`. Deploy.
Expected: stream reaches `[Approve] approval_required` and PAUSES; the approval bar appears. The secret value `s3cretvalue` appears **nowhere** in the stream (only `••••`). The rendered Secret manifest in the `manifest` event shows `TOKEN: ••••`.

- [ ] **Step 3: Approve and confirm rollout**

Click **Approve**.
Expected: stream continues `[Deploy] … → [Verify] 2/2 ready → endpoint`. Then:
```bash
kubectl get deploy,cm,secret -l app.kubernetes.io/name=demo
kubectl get secret demo-secret -o jsonpath='{.data.TOKEN}' | base64 -d
```
Expected: ConfigMap `demo-config` and Secret `demo-secret` exist; the decoded secret equals `s3cretvalue` (real value IS in the cluster; only the UI/stream is redacted).

- [ ] **Step 4: Confirm Ingress/HPA auto-skip on kind**

Redeploy `demo` with `ingress_host=demo.local` and `hpa_enabled=true` (use curl):
```bash
curl -s -X POST localhost:8000/deploy -H 'Content-Type: application/json' \
  -d '{"name":"demo","image":"mendhak/http-https-echo:31","port":8080,"replicas":2,"mode":"autonomous","ingress_host":"demo.local","hpa_enabled":true}'
```
Expected: `info` events "No ingress controller — skipping Ingress" and "No metrics-server — skipping HPA" (kind has neither by default); deploy still succeeds. Confirm none created: `kubectl get ingress,hpa -l app.kubernetes.io/name=demo` → No resources.

- [ ] **Step 5: Negative — invalid name rejected at the API**

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST localhost:8000/deploy \
  -H 'Content-Type: application/json' -d '{"name":"--evil","image":"x:1"}'
```
Expected: `422` (RFC1123 validator rejects before anything runs).

- [ ] **Step 6: Cleanup**

```bash
helm uninstall demo -n default
```

---

## Self-review notes

- **Spec coverage:** ConfigMap/Secret/Ingress/HPA/PDB (§6) ✔; secret redaction everywhere in the stream, raw values only in-cluster (§7.1) ✔; kube-score gate (§4 step 5) ✔; manual approval gate + autonomous mode (§5) ✔; detect-don't-assume for Ingress/HPA (§13) ✔; Phase-0 review fixes: RFC1123 input validation (flag-injection) + non-colliding managed-by label ✔.
- **Deferred:** NetworkPolicy default-deny (§6 "where applicable") — defer to a later hardening pass; ResourceQuota/LimitRange — Phase 5 multi-tenant. Note in ledger.
- **Type consistency:** `coordinator.run(cfg, bus, approvals)` used identically in main and tests; `Approvals.create/resolve`, `guardrails.secret_variants/redact`, `deploy.detect_capabilities`, `build_values` nested keys all match across tasks.
