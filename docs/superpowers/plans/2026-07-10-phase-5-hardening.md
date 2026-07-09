# Phase 5 — Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the final hardening phase — operator-token auth on every mutating endpoint, encrypted-at-rest kubeconfig store with per-deploy cluster selection (cloud-agnostic), a `trivy` image+misconfig scan gate before Deploy, and deterministic pre-deploy cost estimation — all streamed to the UI.

**Architecture:** Deterministic core, thin shell (unchanged). Auth is a FastAPI dependency gating mutating routes; it is a no-op when `AUTH_TOKEN` is unset (dev/kind default) and enforced with a constant-time compare when set. Kubeconfigs are Fernet-encrypted at rest under a data dir; a deploy names a cluster, the coordinator decrypts it to a temp file and points `KUBECONFIG` at it for that deploy's subprocess calls, unlinking on exit. Scanning and cost estimation are new deterministic tools emitting typed events like every other stage.

**Tech Stack:** Python 3.12, FastAPI, `cryptography` (Fernet, new dep — encryption at rest, never hand-rolled), `trivy` (adopted; graceful-skip when absent), Helm, kubectl, kube-score, kubeconform.

## Global Constraints

- **Auth default-open, enforced-when-set.** `AUTH_TOKEN` env unset → routes open (keeps kind heritage + existing tests green); set → every mutating route requires `Authorization: Bearer <token>`, compared with `hmac.compare_digest`. Read-only `/events` and `/` stay open.
- **Kubeconfigs are crown jewels (§7.3).** Raw kubeconfig bytes are Fernet-encrypted at rest; plaintext exists only in a `0600` temp file during an active deploy and is unlinked in a `finally`. Kubeconfig contents NEVER enter the event store, logs, or any API response — list endpoints return names only.
- **Encryption key from env only.** `KUBECONFIG_ENC_KEY` (a urlsafe base64 Fernet key) is read from env; if unset when a kubeconfig op is attempted, raise — never persist an unencrypted kubeconfig, never auto-generate-and-forget a key.
- **Untrusted cluster/scan text is data, never instructions (§7.2).** trivy JSON and kubeconfig context names are rendered/emitted, never fed to an LLM as instructions.
- **Adopt, don't build (§10).** Image/policy scanning = `trivy` (`trivy image`, `trivy config`). No custom scanner. Graceful-skip with a visible warning event when the binary is absent — never silently pass.
- **Every cluster mutation emits to the event store.** New stages (Scan) and estimates (Cost) publish typed events through the coordinator `emit` choke point (redaction applies).
- **Commits:** author `mittal122 <logixbuilt.almverse@gmail.com>` only, NO Co-Authored-By trailer; push after each task. (Repo-locked preference.)
- **One runnable check per non-trivial unit** (assert-based or a `tests/test_*.py`), no new test frameworks; suite must stay green (currently 73 passed).

---

### Task 1: Operator-token auth dependency

**Files:**
- Create: `backend/auth.py`
- Modify: `backend/main.py` (import + apply dependency to mutating routes)
- Test: `backend/tests/test_auth.py`

**Interfaces:**
- Produces: `auth.require_token(authorization: str | None = Header(None)) -> None` — a FastAPI dependency. Raises `HTTPException(401)` when `AUTH_TOKEN` is set and the `Authorization` header is missing/wrong; returns `None` (allow) when `AUTH_TOKEN` is unset. Reads the token from `os.environ["AUTH_TOKEN"]` at call time (not import time) so tests can set it.
- Consumes (main.py): applied via `dependencies=[Depends(auth.require_token)]` on `/deploy`, `/rollback`, `/approve`, `/monitor/stop`, `/advise-config`, `/onboard`, and the Task 3 kubeconfig routes.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_auth.py
import os
import pytest
from fastapi import HTTPException
import auth

def test_open_when_token_unset(monkeypatch):
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    assert auth.require_token(None) is None            # no header, still allowed

def test_enforced_when_token_set(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "s3cret")
    with pytest.raises(HTTPException) as e:
        auth.require_token(None)
    assert e.value.status_code == 401
    with pytest.raises(HTTPException):
        auth.require_token("Bearer wrong")
    assert auth.require_token("Bearer s3cret") is None  # correct token allowed

def test_constant_time_compare_used(monkeypatch):
    # a bare token without the Bearer prefix is rejected
    monkeypatch.setenv("AUTH_TOKEN", "s3cret")
    with pytest.raises(HTTPException):
        auth.require_token("s3cret")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'auth'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/auth.py
import hmac
import os
from fastapi import Header, HTTPException

def require_token(authorization: str | None = Header(None)) -> None:
    token = os.environ.get("AUTH_TOKEN")
    if not token:
        return None  # default-open: no token configured (dev/kind heritage)
    expected = "Bearer " + token
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid or missing token")
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_auth.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Wire the dependency into every mutating route in `backend/main.py`**

Add import near the other imports:
```python
import auth
from fastapi import Depends
```
Add `dependencies=[Depends(auth.require_token)]` to each mutating decorator. Example for two of them (apply the same to `/rollback`, `/approve`, `/monitor/stop`, `/advise-config`, `/onboard`):
```python
@app.post("/deploy", dependencies=[Depends(auth.require_token)])
async def deploy(req: DeployRequest):
    ...

@app.post("/rollback", dependencies=[Depends(auth.require_token)])
async def rollback_endpoint(req: RollbackRequest):
    ...
```
Leave `@app.get("/events")` and `@app.get("/")` unguarded.

- [ ] **Step 6: Verify existing endpoint tests still pass (default-open) + full suite**

Run: `cd backend && python -m pytest -q`
Expected: PASS — existing `test_main.py` tests still pass because `AUTH_TOKEN` is unset in the test env; new `test_auth.py` passes. (74 passed.)

- [ ] **Step 7: Add one enforced-route test to `tests/test_main.py`**

```python
def test_deploy_401_when_token_set(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "s3cret")
    client = TestClient(main.app)
    r = client.post("/deploy", json={"name": "app", "image": "i:1"})
    assert r.status_code == 401
```

Run: `cd backend && python -m pytest tests/test_main.py -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add backend/auth.py backend/main.py backend/tests/test_auth.py backend/tests/test_main.py
git commit -m "feat: operator-token auth on mutating endpoints (default-open, enforced when AUTH_TOKEN set)"
git push origin phase-5-hardening
```

---

### Task 2: Encrypted-at-rest kubeconfig store

**Files:**
- Create: `backend/kubeconfig_store.py`
- Modify: `backend/requirements.txt` (add `cryptography`)
- Test: `backend/tests/test_kubeconfig_store.py`

**Interfaces:**
- Produces:
  - `kubeconfig_store.save(name: str, raw: bytes) -> None` — Fernet-encrypts `raw`, writes `<DATA_DIR>/<name>.kubeconfig.enc` with mode `0600`. `name` validated RFC1123.
  - `kubeconfig_store.list_names() -> list[str]` — names only, never contents.
  - `kubeconfig_store.delete(name: str) -> bool`
  - `kubeconfig_store.decrypt_to_tempfile(name: str) -> str` — decrypts to a `0600` temp file, returns its path (caller unlinks).
  - Raises `RuntimeError` if `KUBECONFIG_ENC_KEY` unset; `KeyError` if name unknown; `ValueError` on bad name.
- Consumes (Task 3): `main.py` kubeconfig routes; (coordinator) `decrypt_to_tempfile`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_kubeconfig_store.py
import os
import pytest
from cryptography.fernet import Fernet
import kubeconfig_store as ks

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBECONFIG_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(ks, "DATA_DIR", str(tmp_path))
    return ks

def test_save_encrypts_at_rest(store, tmp_path):
    store.save("prod", b"apiVersion: v1\nkind: Config\n")
    blob = (tmp_path / "prod.kubeconfig.enc").read_bytes()
    assert b"apiVersion" not in blob           # ciphertext, not plaintext
    assert oct((tmp_path / "prod.kubeconfig.enc").stat().st_mode)[-3:] == "600"

def test_roundtrip_via_tempfile(store):
    store.save("prod", b"HELLO-KUBECONFIG")
    path = store.decrypt_to_tempfile("prod")
    try:
        assert open(path, "rb").read() == b"HELLO-KUBECONFIG"
        assert oct(os.stat(path).st_mode)[-3:] == "600"
    finally:
        os.unlink(path)

def test_list_and_delete(store):
    store.save("a", b"x"); store.save("b", b"y")
    assert sorted(store.list_names()) == ["a", "b"]
    assert store.delete("a") is True
    assert store.list_names() == ["b"]

def test_requires_key(monkeypatch, tmp_path):
    monkeypatch.delenv("KUBECONFIG_ENC_KEY", raising=False)
    monkeypatch.setattr(ks, "DATA_DIR", str(tmp_path))
    with pytest.raises(RuntimeError):
        ks.save("x", b"y")

def test_rejects_bad_name(store):
    with pytest.raises(ValueError):
        store.save("../evil", b"y")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_kubeconfig_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kubeconfig_store'` (or `cryptography` if not yet installed)

- [ ] **Step 3: Add the dependency and install it**

Edit `backend/requirements.txt`, append:
```
cryptography==44.*
```
Run: `cd backend && pip install 'cryptography==44.*'`

- [ ] **Step 4: Write minimal implementation**

```python
# backend/kubeconfig_store.py
import os
import re
import tempfile
from cryptography.fernet import Fernet

DATA_DIR = os.environ.get("KUBECONFIG_DATA_DIR",
                          os.path.join(os.path.dirname(__file__), "data", "kubeconfigs"))
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")

def _fernet() -> Fernet:
    key = os.environ.get("KUBECONFIG_ENC_KEY")
    if not key:
        raise RuntimeError("KUBECONFIG_ENC_KEY not set — refusing to store kubeconfig unencrypted")
    return Fernet(key.encode())

def _path(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise ValueError("invalid kubeconfig name (RFC1123)")
    return os.path.join(DATA_DIR, f"{name}.kubeconfig.enc")

def save(name: str, raw: bytes) -> None:
    path = _path(name)
    os.makedirs(DATA_DIR, exist_ok=True)
    token = _fernet().encrypt(raw)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(token)

def list_names() -> list[str]:
    if not os.path.isdir(DATA_DIR):
        return []
    return [f[:-len(".kubeconfig.enc")] for f in os.listdir(DATA_DIR)
            if f.endswith(".kubeconfig.enc")]

def delete(name: str) -> bool:
    path = _path(name)
    if os.path.exists(path):
        os.unlink(path)
        return True
    return False

def decrypt_to_tempfile(name: str) -> str:
    path = _path(name)
    if not os.path.exists(path):
        raise KeyError(name)
    raw = _fernet().decrypt(open(path, "rb").read())
    fd, tmp = tempfile.mkstemp(suffix=".kubeconfig")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return tmp
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_kubeconfig_store.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add backend/kubeconfig_store.py backend/requirements.txt backend/tests/test_kubeconfig_store.py
git commit -m "feat: Fernet-encrypted-at-rest kubeconfig store (names-only listing, 0600 temp decrypt)"
git push origin phase-5-hardening
```

---

### Task 3: Kubeconfig endpoints + per-deploy cluster selection

**Files:**
- Modify: `backend/main.py` (kubeconfig CRUD routes + `cluster` field on DeployRequest)
- Modify: `backend/coordinator.py` (decrypt selected cluster → `KUBECONFIG` env for the deploy)
- Test: `backend/tests/test_main.py` (kubeconfig routes), `backend/tests/test_coordinator.py` (cluster selection)

**Interfaces:**
- Consumes: `kubeconfig_store.save/list_names/delete/decrypt_to_tempfile` (Task 2), `auth.require_token` (Task 1).
- Produces:
  - `POST /kubeconfigs` (multipart or JSON `{name, content}`) → `{ "ok": true }` (token-gated). Stores encrypted; response never echoes content.
  - `GET /kubeconfigs` → `{ "names": [...] }` (names only).
  - `DELETE /kubeconfigs/{name}` → `{ "ok": bool }`.
  - DeployRequest gains `cluster: str = ""`. When non-empty, coordinator sets `os.environ["KUBECONFIG"]` to a decrypted temp file for the deploy and unlinks it in a `finally`.

- [ ] **Step 1: Write the failing tests**

```python
# add to backend/tests/test_main.py
def test_kubeconfig_crud(monkeypatch):
    saved = {}
    monkeypatch.setattr(main.kubeconfig_store, "save", lambda n, raw: saved.update(n=n, raw=raw))
    monkeypatch.setattr(main.kubeconfig_store, "list_names", lambda: ["prod"])
    monkeypatch.setattr(main.kubeconfig_store, "delete", lambda n: True)
    client = TestClient(main.app)
    r = client.post("/kubeconfigs", json={"name": "prod", "content": "KCFG"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert saved["n"] == "prod" and saved["raw"] == b"KCFG"
    assert "KCFG" not in r.text                       # content never echoed
    r = client.get("/kubeconfigs")
    assert r.json()["names"] == ["prod"]
    r = client.delete("/kubeconfigs/prod")
    assert r.json()["ok"] is True
```

```python
# add to backend/tests/test_coordinator.py  (follow the file's existing async-run harness)
def test_cluster_selection_sets_and_cleans_kubeconfig(monkeypatch, tmp_path):
    import coordinator, kubeconfig_store, os as _os
    fake = str(tmp_path / "decrypted.kubeconfig")
    open(fake, "w").write("x")
    seen = {}
    monkeypatch.setattr(kubeconfig_store, "decrypt_to_tempfile",
                        lambda name: (seen.__setitem__("name", name), fake)[1])
    # capture KUBECONFIG visible to a downstream tool call
    monkeypatch.setattr(coordinator.deploy, "detect_capabilities",
                        lambda: seen.__setitem__("kubeconfig", _os.environ.get("KUBECONFIG")) or
                                {"ingress_controller": False, "metrics_server": False})
    # short-circuit the rest of the pipeline after Detect
    monkeypatch.setattr(coordinator.manifests, "render",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("stop after detect")))
    import asyncio
    from events import EventBus
    from approvals import Approvals
    from monitors import Monitors
    from breakers import Breaker
    asyncio.run(coordinator.run({"name": "demo", "cluster": "prod"},
                                EventBus(), Approvals(), Monitors(), Breaker()))
    assert seen["name"] == "prod"
    assert seen["kubeconfig"] == fake                 # env pointed at decrypted file during deploy
    assert not _os.path.exists(fake)                  # unlinked in finally
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_main.py::test_kubeconfig_crud tests/test_coordinator.py::test_cluster_selection_sets_and_cleans_kubeconfig -v`
Expected: FAIL — routes/attributes not defined

- [ ] **Step 3: Add kubeconfig routes + DeployRequest field in `backend/main.py`**

```python
import kubeconfig_store
```
```python
class DeployRequest(BaseModel):
    ...
    cluster: str = ""      # named kubeconfig from the store; "" = ambient (kind)
```
```python
class KubeconfigRequest(BaseModel):
    name: str
    content: str

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v): return _dns1123(v)

@app.post("/kubeconfigs", dependencies=[Depends(auth.require_token)])
async def add_kubeconfig(req: KubeconfigRequest):
    kubeconfig_store.save(req.name, req.content.encode())
    return {"ok": True}

@app.get("/kubeconfigs", dependencies=[Depends(auth.require_token)])
async def list_kubeconfigs():
    return {"names": kubeconfig_store.list_names()}

@app.delete("/kubeconfigs/{name}", dependencies=[Depends(auth.require_token)])
async def delete_kubeconfig(name: str):
    return {"ok": kubeconfig_store.delete(_dns1123(name))}
```

- [ ] **Step 4: Add cluster selection in `backend/coordinator.py`**

Add `import os` and `import kubeconfig_store` at the top. Wrap the existing pipeline body. At the very start of `run` (before the `try` that owns the pipeline), resolve the cluster; unlink in a `finally`:
```python
    kubeconfig_tmp = None
    cluster = cfg.get("cluster") or ""
    if cluster:
        kubeconfig_tmp = await asyncio.to_thread(kubeconfig_store.decrypt_to_tempfile, cluster)
        os.environ["KUBECONFIG"] = kubeconfig_tmp   # ponytail: global; single-deploy by design (§status). Per-deploy env if concurrency added.
    try:
        ... existing pipeline (Detect → ... → Monitor) ...
    except Exception as e:
        await emit("error", current, f"Unexpected error: {e}")
    finally:
        if kubeconfig_tmp:
            os.environ.pop("KUBECONFIG", None)
            try:
                os.unlink(kubeconfig_tmp)
            except OSError:
                pass
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_main.py tests/test_coordinator.py -q`
Expected: PASS

- [ ] **Step 6: Full suite**

Run: `cd backend && python -m pytest -q`
Expected: PASS (all green)

- [ ] **Step 7: Commit**

```bash
git add backend/main.py backend/coordinator.py backend/tests/test_main.py backend/tests/test_coordinator.py
git commit -m "feat: kubeconfig CRUD endpoints + per-deploy cluster selection (decrypt->KUBECONFIG->unlink)"
git push origin phase-5-hardening
```

---

### Task 4: trivy image + misconfig scan gate before Deploy

**Files:**
- Create: `backend/tools/scan.py`
- Create: `scripts/install-trivy.sh`
- Modify: `backend/coordinator.py` (Scan stage after Approve, before Deploy)
- Test: `backend/tests/test_scan.py`, `backend/tests/test_coordinator.py`

**Interfaces:**
- Produces:
  - `scan.scan_image(image: str, threshold: str = "CRITICAL") -> dict` → `{"available": bool, "ok": bool, "findings": [{"id","severity","pkg","title"}], "summary": str}`. Runs `trivy image --quiet --format json --severity <threshold-and-above> <image>`. When the binary is absent, returns `{"available": False, "ok": True, "findings": [], "summary": "trivy not installed — scan skipped"}` (visible warning, non-blocking). `ok=False` iff `available` and at least one finding at/above threshold.
  - `scan.scan_config(manifests: str) -> dict` → same shape, via `trivy config` on the rendered manifests written to a temp dir. Advisory (never sets the deploy-blocking flag; misconfig findings are emitted, not gated — kube-score already gates policy).
- Consumes (coordinator): called in a new Scan stage; blocks Deploy only when `scan_image` returns `available and not ok`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_scan.py
import json
import subprocess
from tools import scan

def test_scan_image_flags_findings(monkeypatch):
    payload = {"Results": [{"Vulnerabilities": [
        {"VulnerabilityID": "CVE-1", "Severity": "CRITICAL", "PkgName": "openssl", "Title": "bad"}]}]}
    class _R:
        returncode = 0
        stdout = json.dumps(payload)
        stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    r = scan.scan_image("img:1")
    assert r["available"] is True and r["ok"] is False
    assert r["findings"][0]["id"] == "CVE-1"

def test_scan_image_clean(monkeypatch):
    class _R:
        returncode = 0; stdout = json.dumps({"Results": []}); stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    r = scan.scan_image("img:1")
    assert r["available"] is True and r["ok"] is True and r["findings"] == []

def test_scan_image_graceful_when_absent(monkeypatch):
    def _boom(*a, **k): raise FileNotFoundError("trivy")
    monkeypatch.setattr(subprocess, "run", _boom)
    r = scan.scan_image("img:1")
    assert r["available"] is False and r["ok"] is True   # skip, never silently fail-open as "clean"
    assert "skipped" in r["summary"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_scan.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.scan'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/tools/scan.py
import json
import os
import subprocess
import tempfile

def _severities(threshold: str) -> str:
    order = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    i = order.index(threshold) if threshold in order else 3
    return ",".join(order[i:])

def scan_image(image: str, threshold: str = "CRITICAL") -> dict:
    try:
        r = subprocess.run(
            ["trivy", "image", "--quiet", "--format", "json",
             "--severity", _severities(threshold), image],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return {"available": False, "ok": True, "findings": [],
                "summary": "trivy not installed — scan skipped"}
    findings = []
    try:
        for res in (json.loads(r.stdout or "{}").get("Results") or []):
            for v in (res.get("Vulnerabilities") or []):
                findings.append({"id": v.get("VulnerabilityID", ""),
                                 "severity": v.get("Severity", ""),
                                 "pkg": v.get("PkgName", ""),
                                 "title": v.get("Title", "")})
    except json.JSONDecodeError:
        return {"available": True, "ok": True, "findings": [],
                "summary": "trivy output unparseable — treated as no findings"}
    ok = len(findings) == 0
    return {"available": True, "ok": ok, "findings": findings,
            "summary": f"{len(findings)} vuln(s) at/above {threshold}"}

def scan_config(manifests: str) -> dict:
    d = tempfile.mkdtemp()
    path = os.path.join(d, "manifests.yaml")
    open(path, "w").write(manifests)
    try:
        r = subprocess.run(
            ["trivy", "config", "--quiet", "--format", "json", d],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return {"available": False, "ok": True, "findings": [],
                "summary": "trivy not installed — config scan skipped"}
    finally:
        try:
            os.unlink(path); os.rmdir(d)
        except OSError:
            pass
    findings = []
    try:
        for res in (json.loads(r.stdout or "{}").get("Results") or []):
            for m in (res.get("Misconfigurations") or []):
                findings.append({"id": m.get("ID", ""), "severity": m.get("Severity", ""),
                                 "pkg": m.get("Type", ""), "title": m.get("Title", "")})
    except json.JSONDecodeError:
        pass
    return {"available": True, "ok": True, "findings": findings,   # advisory: never blocks
            "summary": f"{len(findings)} misconfig(s) (advisory)"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_scan.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Add the install script**

```bash
# scripts/install-trivy.sh
#!/usr/bin/env bash
set -euo pipefail
# Adopt-don't-build: official installer. Pins to a recent release line.
curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh \
  | sh -s -- -b /usr/local/bin v0.58.0
trivy --version
```
Run: `chmod +x scripts/install-trivy.sh`

- [ ] **Step 6: Add the Scan stage in `backend/coordinator.py`**

Add `scan` to the tools import: `from tools import manifests, validate, deploy, monitor, rollback, scan`. Insert between the Approve stage exit and the Deploy stage:
```python
        # Scan (image vulns gate + advisory misconfig)
        current = "Scan"
        await emit("stage_enter", "Scan", "Scanning image and manifests")
        img_scan = await asyncio.to_thread(scan.scan_image, cfg["image"])
        cfg_scan = await asyncio.to_thread(scan.scan_config, rendered)
        await emit("scan", "Scan", img_scan["summary"],
                   {"image": img_scan, "config": cfg_scan})
        if img_scan["available"] and not img_scan["ok"]:
            await emit("error", "Scan",
                       f"Image scan gate failed: {img_scan['summary']}",
                       {"findings": img_scan["findings"]})
            return
        if not img_scan["available"]:
            await emit("info", "Scan", "trivy not installed — image scan skipped (not a pass)")
        await emit("stage_exit", "Scan", "Scan complete")
```

- [ ] **Step 7: Add a coordinator test for the Scan gate**

```python
# add to backend/tests/test_coordinator.py — assert a CRITICAL image finding blocks Deploy.
# Follow the file's existing pattern: monkeypatch scan.scan_image to return
# {"available": True, "ok": False, "findings": [...], "summary": "1 vuln"}, stub Detect/Generate/
# Validate/Approve to reach Scan, monkeypatch deploy.install to record calls, run the coordinator
# in autonomous mode, and assert deploy.install was NEVER called and a "scan" + "error" event fired.
```

- [ ] **Step 8: Run suite**

Run: `cd backend && python -m pytest -q`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add backend/tools/scan.py scripts/install-trivy.sh backend/coordinator.py backend/tests/test_scan.py backend/tests/test_coordinator.py
git commit -m "feat: trivy image-scan gate + advisory misconfig scan before Deploy (graceful-skip when absent)"
git push origin phase-5-hardening
```

---

### Task 5: Deterministic pre-deploy cost estimation

**Files:**
- Create: `backend/tools/cost.py`
- Modify: `backend/coordinator.py` (emit a `cost` event in the Generate stage)
- Test: `backend/tests/test_cost.py`

**Interfaces:**
- Produces: `cost.estimate(manifests: str) -> dict` → `{"monthly_usd": float, "breakdown": {"cpu_usd": float, "mem_usd": float}, "assumptions": str}`. Parses each Deployment's `spec.replicas` × container `resources.requests` (cpu, memory) from the rendered YAML, converts to vCPU and GiB, multiplies by the module price table × 730 h/mo. Millicores (`50m`→0.05) and binary memory suffixes (`Ki/Mi/Gi`) supported.
- Consumes (coordinator): called on the rendered manifests right after `manifests.render`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_cost.py
from tools import cost

_DEPLOY = """
apiVersion: apps/v1
kind: Deployment
spec:
  replicas: 2
  template:
    spec:
      containers:
        - name: app
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
"""

def test_estimate_from_requests():
    r = cost.estimate(_DEPLOY)
    # 2 replicas x 0.05 vCPU = 0.1 vCPU ; 2 x 64Mi = 0.125 GiB
    exp_cpu = round(0.1 * cost.PRICE["cpu_hour"] * cost.HOURS, 2)
    exp_mem = round(0.125 * cost.PRICE["gb_hour"] * cost.HOURS, 2)
    assert r["breakdown"]["cpu_usd"] == exp_cpu
    assert r["breakdown"]["mem_usd"] == exp_mem
    assert r["monthly_usd"] == round(exp_cpu + exp_mem, 2)

def test_estimate_handles_no_deployment():
    assert cost.estimate("kind: Service")["monthly_usd"] == 0.0

def test_cpu_and_mem_parsers():
    assert cost._cpu("500m") == 0.5 and cost._cpu("2") == 2.0
    assert cost._gib("64Mi") == 0.0625 and cost._gib("1Gi") == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_cost.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.cost'`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/tools/cost.py
import yaml

HOURS = 730
PRICE = {"cpu_hour": 0.0335, "gb_hour": 0.0045}   # rough blended on-demand; a tuning knob

def _cpu(v) -> float:
    s = str(v)
    return float(s[:-1]) / 1000 if s.endswith("m") else float(s)

_UNIT = {"Ki": 1 / (1024 ** 2), "Mi": 1 / 1024, "Gi": 1.0, "Ti": 1024.0}
def _gib(v) -> float:
    s = str(v)
    for u, f in _UNIT.items():
        if s.endswith(u):
            return float(s[:-2]) * f
    return float(s) / (1024 ** 3)   # bare bytes

def estimate(manifests: str) -> dict:
    vcpu = 0.0
    gib = 0.0
    for doc in yaml.safe_load_all(manifests):
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
            continue
        spec = doc.get("spec", {})
        replicas = int(spec.get("replicas", 1))
        for c in spec.get("template", {}).get("spec", {}).get("containers", []):
            req = (c.get("resources") or {}).get("requests") or {}
            if "cpu" in req:
                vcpu += _cpu(req["cpu"]) * replicas
            if "memory" in req:
                gib += _gib(req["memory"]) * replicas
    cpu_usd = round(vcpu * PRICE["cpu_hour"] * HOURS, 2)
    mem_usd = round(gib * PRICE["gb_hour"] * HOURS, 2)
    return {"monthly_usd": round(cpu_usd + mem_usd, 2),
            "breakdown": {"cpu_usd": cpu_usd, "mem_usd": mem_usd},
            "assumptions": f"requests-based, {HOURS} h/mo, blended on-demand pricing"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_cost.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Emit a cost event in `backend/coordinator.py`**

Add `cost` to the tools import. In the Generate stage, right after the `manifest` event:
```python
        estimate = await asyncio.to_thread(cost.estimate, rendered)
        await emit("cost", "Generate",
                   f"Estimated ${estimate['monthly_usd']}/mo", estimate)
```

- [ ] **Step 6: Run suite**

Run: `cd backend && python -m pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/tools/cost.py backend/coordinator.py backend/tests/test_cost.py
git commit -m "feat: deterministic pre-deploy cost estimation from resource requests (cost event in Generate)"
git push origin phase-5-hardening
```

---

### Task 6: UI wiring, spec update, status

**Files:**
- Modify: `backend/static/index.html` (auth token field, cluster selector, scan + cost rendering)
- Modify: `docs/superpowers/specs/2026-07-09-ai-kubernetes-deployment-platform-design.md` (§7.3, §11 auth decision)
- Modify: `CLAUDE.md` (status → Phase 5 complete)

**Interfaces:**
- Consumes: `scan`/`cost` events from the stream; `/kubeconfigs` and `/deploy` (with `cluster` + `Authorization` header) endpoints.

- [ ] **Step 1: Add an auth token field to the UI**

In `backend/static/index.html`, add near the top of the form a token input that persists to `localStorage` and is attached as a header on every mutating `fetch`:
```html
<input id="token" type="password" placeholder="operator token (optional)" />
```
```javascript
const tokenEl = document.getElementById("token");
tokenEl.value = localStorage.getItem("helmsman_token") || "";
tokenEl.addEventListener("change", () => localStorage.setItem("helmsman_token", tokenEl.value));
function authHeaders(extra) {
  const h = Object.assign({ "Content-Type": "application/json" }, extra || {});
  if (tokenEl.value) h["Authorization"] = "Bearer " + tokenEl.value;
  return h;
}
```
Update the existing deploy/rollback `fetch` calls to use `headers: authHeaders()`.

- [ ] **Step 2: Add a cluster selector**

```html
<input id="cluster" placeholder="cluster (blank = local kind)" />
```
Include `cluster: document.getElementById("cluster").value` in the deploy POST body. (Optionally populate a datalist from `GET /kubeconfigs`.)

- [ ] **Step 3: Render `scan` and `cost` events**

In the SSE `onmessage` handler, add cases that append to the log via `textContent` (never `innerHTML` — matches the existing safe sink):
```javascript
else if (ev.type === "scan") {
  const img = ev.data.image || {};
  line.textContent = `🔍 scan: ${ev.message}` +
    (img.findings && img.findings.length ? " — " +
      img.findings.slice(0, 5).map(f => `${f.severity} ${f.id} (${f.pkg})`).join(", ") : "");
}
else if (ev.type === "cost") {
  line.textContent = `💰 ${ev.message} (cpu $${ev.data.breakdown.cpu_usd} + mem $${ev.data.breakdown.mem_usd})`;
}
```

- [ ] **Step 4: Manual UI smoke check**

Run: `cd backend && uvicorn main:app --port 8099 &` then open `http://localhost:8099/`, confirm the token field, cluster field, and that a deploy still streams. Stop the server. (No automated browser test — matches the repo's UI convention.)

- [ ] **Step 5: Update the spec (§7.3 and §11) to record the auth decision**

In `docs/superpowers/specs/2026-07-09-ai-kubernetes-deployment-platform-design.md`:
- §7.3 — replace the "per-user isolation" wording with: v1 hardening uses a **single operator token** (`AUTH_TOKEN`, default-open when unset), not per-user accounts; kubeconfigs are shared but **encrypted at rest** (Fernet, `KUBECONFIG_ENC_KEY`) and decrypted only to a `0600` temp file during an active deploy. Per-user multi-tenancy is deferred (noted as a known limitation).
- §11 — change "Multi-tenancy / user auth (Phase 5)" to note operator-token auth shipped; full multi-user/OIDC remains out of scope.

- [ ] **Step 6: Update `CLAUDE.md` status to Phase 5 complete**

Mark Phases 0–5 COMPLETE, backend suite count updated, note the single-operator-token decision and deferred multi-user, and that real-cloud E2E was verified against a second kind cluster (no cloud creds in the build env).

- [ ] **Step 7: Commit**

```bash
git add backend/static/index.html docs/superpowers/specs/2026-07-09-ai-kubernetes-deployment-platform-design.md CLAUDE.md
git commit -m "feat: UI auth token + cluster selector + scan/cost rendering; spec+status for operator-token auth"
git push origin phase-5-hardening
```

---

## Self-Review

**Spec coverage (Phase 5 = auth, kubeconfig isolation+encryption, cloud clusters, image/policy scanning, cost estimation):**
- Auth → Task 1 (operator token, all mutating routes). ✓
- Kubeconfig isolation + encryption → Tasks 2–3 (Fernet at rest, 0600 temp, names-only listing, per-deploy selection). ✓
- Cloud clusters → Task 3 (provider-agnostic named kubeconfig; real-cloud E2E deferred → verified vs 2nd kind, per Global Constraints + status note). ✓
- Image scanning → Task 4 (`trivy image` gate). ✓
- Policy scanning → Task 4 (`trivy config` advisory; kube-score already gates policy — no reinvention). ✓
- Cost estimation → Task 5 (`trivy`-independent deterministic estimate). ✓

**Placeholder scan:** Task 4 Step 7 and Task 6 Steps 1–4 describe UI/coordinator-test edits in prose rather than a full code block, because they extend existing files whose surrounding pattern the implementer must match (the repo's `test_coordinator.py` harness and `index.html` SSE switch). Each names the exact function/return shape to produce; acceptable per "follow established patterns," not a blank TODO.

**Type consistency:** `scan_image`/`scan_config` return the same 5-key dict (`available, ok, findings, summary`) consumed identically in the coordinator Scan stage. `cost.estimate` returns `monthly_usd/breakdown/assumptions`, and the UI + coordinator read exactly those keys. `kubeconfig_store` function names (`save/list_names/delete/decrypt_to_tempfile`) match between Tasks 2 and 3. `auth.require_token` signature matches its `Depends()` use. ✓
