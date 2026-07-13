import asyncio
import json
import os
import re
import subprocess
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel, field_validator, model_validator, Field
import auth
import kubeconfig_store
from events import Event, EventBus
from coordinator import run as coordinator_run
from approvals import Approvals
from monitors import Monitors
from agents import onboarding, config_advisor
from tools import rollback, cluster, portforward, builder, compose
import intake
from breakers import Breaker
import logging
import store

FORWARD_TTL_S = 30          # a forward not heartbeated within this is reaped
FORWARD_REAP_INTERVAL_S = 10
COOKIE_SECURE = os.environ.get("COOKIE_INSECURE") != "1"   # Secure by default; set COOKIE_INSECURE=1 for local http

logging.basicConfig(level=logging.INFO,
                    format='{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
log = logging.getLogger("helmsman")

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(_app):
    backend = await store.init()
    log.info(f"store backend: {backend}")
    # bootstrap the first admin from env (only if no users exist yet)
    be, bp = os.environ.get("BOOTSTRAP_ADMIN_EMAIL"), os.environ.get("BOOTSTRAP_ADMIN_PASSWORD")
    be = be.strip().lower() if be else be
    if be and bp and await store.user_count() == 0:
        try:
            await store.user_create(be, auth.hash_password(bp), "admin")
            log.info(f"bootstrapped admin user: {be}")
        except Exception as e:
            log.info(f"bootstrap admin skipped: {e}")
    async def reaper():
        while True:
            await asyncio.sleep(FORWARD_REAP_INTERVAL_S)
            try:
                await asyncio.to_thread(portforward.reap, FORWARD_TTL_S)
            except Exception:
                pass
    task = asyncio.create_task(reaper())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.to_thread(portforward.stop_all)   # never orphan a port-forward
        await store.close()

app = FastAPI(title="Helmsman", version="1.0", lifespan=lifespan)
bus = EventBus()
approvals = Approvals()
monitors = Monitors()
breakers = Breaker()
_bg_tasks: set = set()
STATIC = os.path.join(os.path.dirname(__file__), "static")

_RFC1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?\Z")

def _dns1123(v: str) -> str:
    if not _RFC1123.match(v) or len(v) > 63:
        raise ValueError("must be a valid RFC1123 name (lowercase alphanumeric/-, no leading -)")
    return v

class DeployRequest(BaseModel):
    name: str
    image: str = ""        # pre-built image; OR build from git_repo below
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
    cluster: str = ""      # named kubeconfig from the store; "" = ambient (kind)
    git_repo: str = ""     # deploy-from-source: clone + build this repo's Dockerfile
    git_branch: str = ""
    git_ref: str = ""      # commit sha / tag (optional)
    dockerfile: str = ""   # "" = auto-detect in the repo (root Dockerfile / sole match / else list)
    compose: str = ""      # multi-service: raw docker-compose YAML (deploys the whole stack)
    compose_path: str = "" # or read the compose file at this path inside git_repo
    compose_env: dict[str, str] = {}  # values for ${VAR} interpolation in the compose file (e.g. TAG, POSTGRES_PASSWORD)
    services: list[dict] = []  # structured-intake path: pre-normalized per-service cfgs (from POST /intake/ingest)
    allow_vulnerable: bool = False  # operator override: proceed even if the image scan gate finds CRITICAL/HIGH vulns

    @field_validator("compose_path")
    @classmethod
    def _valid_compose_path(cls, v):
        if v and (".." in v or v.startswith("/")):
            raise ValueError("invalid compose_path")
        return v

    @field_validator("name", "namespace")
    @classmethod
    def _valid_name(cls, v): return _dns1123(v)

    @field_validator("image")
    @classmethod
    def _valid_image(cls, v):
        if v.startswith("-") or any(c.isspace() for c in v):
            raise ValueError("invalid image reference")
        return v

    @field_validator("git_repo")
    @classmethod
    def _valid_repo(cls, v):
        if v and not builder.valid_url(v):
            raise ValueError("invalid git repo URL (must be https://… or git@…)")
        return v

    @field_validator("git_branch", "git_ref")
    @classmethod
    def _valid_ref(cls, v):
        if v and not builder.valid_ref(v):
            raise ValueError("invalid git ref (safe chars only, no leading '-')")
        return v

    @model_validator(mode="after")
    def _image_or_repo(self):
        if self.compose_path and not self.git_repo:
            raise ValueError("compose_path needs git_repo")
        if not self.image and not self.git_repo and not self.compose and not self.services:
            raise ValueError("provide 'image', 'git_repo', 'compose', or 'services'")
        return self

class DockerfilesRequest(BaseModel):
    git_repo: str
    git_branch: str = ""
    git_ref: str = ""

    @field_validator("git_repo")
    @classmethod
    def _valid_repo(cls, v):
        if not builder.valid_url(v):
            raise ValueError("invalid git repo URL (must be https://… or git@…)")
        return v

    @field_validator("git_branch", "git_ref")
    @classmethod
    def _valid_ref(cls, v):
        if v and not builder.valid_ref(v):
            raise ValueError("invalid git ref (safe chars only, no leading '-')")
        return v

class ApproveRequest(BaseModel):
    name: str
    approved: bool = True

class MonitorStopRequest(BaseModel):
    name: str

class RollbackRequest(BaseModel):
    name: str
    namespace: str = "default"
    revision: int = Field(gt=0)

    @field_validator("name", "namespace")
    @classmethod
    def _valid_name(cls, v): return _dns1123(v)

class AdviseRequest(BaseModel):
    name: str = ""
    image: str = ""
    port: int = 0
    language_framework: str = ""
    expected_traffic: str = ""
    notes: str = ""

class OnboardRequest(BaseModel):
    app_description: str = ""
    language_framework: str = ""
    start_command: str = ""
    port: int = 0
    notes: str = ""

class KubeconfigRequest(BaseModel):
    name: str
    content: str

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v): return _dns1123(v)

def _read_compose_from_repo(git_repo, branch, ref, path):
    workdir = None
    try:
        workdir, _ = builder.clone(git_repo, branch, ref)
        fp = os.path.join(workdir, path or "docker-compose.yml")
        if not os.path.isfile(fp):
            raise ValueError(f"compose file not found in repo: {path or 'docker-compose.yml'}")
        with open(fp) as f:
            return f.read()
    finally:
        if workdir:
            builder.cleanup(workdir)

@app.post("/deploy", dependencies=[Depends(auth.require_token)])
async def deploy(req: DeployRequest):
    cfg = req.model_dump()
    src = f"image={req.image}"
    # a pasted browser URL (…/tree/<branch>/<subdir>) -> real repo + branch + subdir
    if req.git_repo:
        clean, url_branch, subdir = builder.normalize_repo_url(req.git_repo)
        cfg["git_repo"] = clean
        if url_branch and not req.git_branch:
            cfg["git_branch"] = url_branch
        cfg["git_subdir"] = subdir
    # multi-service: parse the compose stack into per-service cfgs before dispatch
    if req.compose or req.compose_path:
        text = req.compose
        if not text:   # read it from the repo
            subdir = cfg.get("git_subdir", "")
            cpath = req.compose_path or "docker-compose.yml"
            if subdir:                      # tree URL pointed at a subfolder -> look there
                cpath = f"{subdir}/{cpath}"
            try:
                text = await asyncio.to_thread(_read_compose_from_repo,
                    cfg["git_repo"], cfg.get("git_branch", ""), req.git_ref, cpath)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"could not read compose from repo: {e}")
        try:
            services, warnings = compose.parse(text, req.compose_env)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=f"compose parse error: {e}")
        # a build: service needs a repo to build from — inherited from the stack's git_repo.
        # A pasted compose (no git_repo) with build: can't build, so reject it early and clearly.
        if any(s.get("build") for s in services) and not cfg.get("git_repo"):
            raise HTTPException(status_code=422,
                detail="a build: service needs the stack deployed from a Git repo — set git_repo, "
                       "or give each service a pre-built image:")
        cfg["services"], cfg["warnings"] = services, warnings
        src = f"compose={len(services)} services"
    elif req.services:
        # structured-intake path: services are already normalized by intake.ingest; re-validate
        # here so a hand-crafted POST can't smuggle an incomplete/invalid service past the gate.
        try:
            intake.validate_services(req.services)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=f"invalid services: {e}")
        cfg["services"] = req.services
        cfg.setdefault("warnings", [])
        src = f"intake={len(req.services)} services"
    elif req.git_repo:
        src = f"git={builder.display_url(req.git_repo)}"
    task = asyncio.create_task(coordinator_run(cfg, bus, approvals, monitors, breakers))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    await store.append_audit(auth.actor(), "deploy", f"{req.namespace}/{req.name}", True,
                             f"{src} mode={req.mode}")
    return {"deployment_id": req.name}

@app.post("/repo/dockerfiles", dependencies=[Depends(auth.require_token)])
async def repo_dockerfiles(req: DockerfilesRequest):
    """Clone (shallow, no build → runs no repo code) and list Dockerfiles so the UI can
    let the user pick one when the name/location isn't the default ./Dockerfile."""
    def _list():
        workdir = None
        try:
            workdir, sha = builder.clone(req.git_repo, req.git_branch, req.git_ref)
            return builder.list_dockerfiles(workdir), sha
        finally:
            if workdir:
                builder.cleanup(workdir)
    try:
        files, sha = await asyncio.to_thread(_list)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="git clone timed out")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"clone failed: {e}")
    return {"dockerfiles": files, "sha": sha}

@app.post("/rollback", dependencies=[Depends(auth.require_token)])
async def rollback_endpoint(req: RollbackRequest):
    # Manual rollback still emits to the event store — transparency invariant applies to
    # every cluster mutation. No secret values on this path, so no redaction needed.
    await bus.publish(Event(type="command", stage="Rollback",
                            message=f"helm rollback {req.name} {req.revision}",
                            data={"name": req.name, "namespace": req.namespace, "revision": req.revision}))
    try:
        await asyncio.to_thread(rollback.do_rollback, req.name, req.namespace, req.revision)
    except Exception as e:
        await bus.publish(Event(type="error", stage="Rollback", message=f"Manual rollback failed: {e}"))
        return {"ok": False, "error": str(e)}
    await bus.publish(Event(type="remediation", stage="Rollback",
                            message=f"Rolled back {req.name} to revision {req.revision}",
                            data={"revision": req.revision}))
    await store.append_audit(auth.actor(), "rollback", f"{req.namespace}/{req.name}", True,
                             f"revision={req.revision}")
    return {"ok": True}

@app.post("/approve", dependencies=[Depends(auth.require_token)])
async def approve(req: ApproveRequest):
    return {"ok": approvals.resolve(req.name, req.approved)}

@app.post("/monitor/stop", dependencies=[Depends(auth.require_token)])
async def monitor_stop(req: MonitorStopRequest):
    monitors.stop(req.name)
    return {"ok": True}

@app.post("/advise-config", dependencies=[Depends(auth.require_token)])
async def advise_config(req: AdviseRequest):
    return await asyncio.to_thread(config_advisor.advise, req.model_dump())

@app.post("/onboard", dependencies=[Depends(auth.require_token)])
async def onboard(req: OnboardRequest):
    return await asyncio.to_thread(onboarding.generate, req.model_dump())

class IntakePromptRequest(BaseModel):
    app_description: str = ""

class IntakeIngestRequest(BaseModel):
    response: str                      # the JSON blob the developer's AI returned
    name: str = ""
    namespace: str = "default"
    mode: str = "manual"

@app.post("/intake/prompt", dependencies=[Depends(auth.require_token)])
async def intake_prompt(req: IntakePromptRequest):
    # deterministic (no LLM): the fixed structured-intake prompt the developer relays to the
    # AI that built their app, so it returns ALL deploy info in one JSON blob.
    return {"prompt": intake.build_prompt(req.model_dump())}

@app.post("/intake/ingest", dependencies=[Depends(auth.require_token)])
async def intake_ingest(req: IntakeIngestRequest):
    # consume ONLY the structured response -> deploy-ready cfg + Missing list + summary.
    # Never assumes a missing value; the UI asks the user for exactly what's Missing.
    try:
        return intake.ingest(req.response, {"name": req.name, "namespace": req.namespace, "mode": req.mode})
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

@app.post("/kubeconfigs", dependencies=[Depends(auth.require_role("admin"))])
async def add_kubeconfig(req: KubeconfigRequest):
    # admin-gated: registering a cluster credential is a trust escalation (an operator
    # could add a kubeconfig whose SA is cluster-admin elsewhere and deploy to it).
    kubeconfig_store.save(req.name, req.content.encode())
    return {"ok": True}

@app.get("/kubeconfigs", dependencies=[Depends(auth.require_token)])
async def list_kubeconfigs():
    return {"names": kubeconfig_store.list_names()}

@app.delete("/kubeconfigs/{name}", dependencies=[Depends(auth.require_role("admin"))])
async def delete_kubeconfig(name: str):
    try:
        valid = _dns1123(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": kubeconfig_store.delete(valid)}

# ---------- Cluster management (SRE console) — token-gated, error-mapped ----------
_RV = [Depends(auth.require_role("viewer"))]     # read
_RO = [Depends(auth.require_role("operator"))]   # mutate
_RA = [Depends(auth.require_role("admin"))]       # destructive / user admin
_TG = _RO                                          # default gate for mutations

# ---------- auth + user management (RBAC) ----------
class LoginRequest(BaseModel):
    email: str
    password: str

class CreateUserRequest(BaseModel):
    email: str
    password: str = Field(min_length=8)
    role: str = "viewer"

class RoleRequest(BaseModel):
    role: str

@app.post("/auth/login")
async def login(req: LoginRequest):
    email = req.email.strip().lower()
    u = await store.user_get(email)
    # always run one argon2 verify (real hash or dummy) so response time doesn't reveal
    # whether the email exists — closes the user-enumeration timing oracle.
    valid = auth.verify_password(u["pw_hash"] if u else auth.DUMMY_HASH, req.password)
    if not u or not u.get("active", True) or not valid:
        raise HTTPException(status_code=401, detail="invalid email or password")
    token = auth.make_token(u["email"], u["role"])
    await store.append_audit(u["email"], "login", u["email"], True)
    resp = JSONResponse({"token": token, "email": u["email"], "role": u["role"]})
    resp.set_cookie("helmsman_session", token, httponly=True, secure=COOKIE_SECURE,
                    samesite="strict", max_age=auth.JWT_TTL_S)
    return resp

@app.get("/auth/enabled")
async def auth_enabled():
    # is a login ever required? off in zero-config open-dev mode -> the UI hides all
    # login chrome (personal use); on once you set AUTH_TOKEN or create users (selling).
    enabled = await auth._auth_configured() or os.environ.get("ALLOW_OPEN_DEV") != "1"
    return {"enabled": enabled}

@app.get("/auth/me")
async def me(user: dict = Depends(auth.current_user)):
    return {"email": user["email"], "role": user["role"]}

@app.post("/auth/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("helmsman_session", httponly=True, secure=COOKIE_SECURE, samesite="strict")
    return resp

@app.get("/users", dependencies=_RA)
async def users_list():
    return {"users": await store.user_list()}

@app.post("/users", dependencies=_RA)
async def users_create(req: CreateUserRequest):
    if req.role not in auth.ROLES:
        raise HTTPException(status_code=400, detail="role must be viewer|operator|admin")
    email = req.email.strip().lower()
    try:
        u = await store.user_create(email, auth.hash_password(req.password), req.role)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await store.append_audit(auth.actor(), "user_create", req.email, True, f"role={req.role}")
    return {"email": u["email"], "role": u["role"]}

async def _is_last_active_admin(email: str) -> bool:
    admins = [u for u in await store.user_list()
              if u.get("active", True) and u.get("role") == "admin"]
    return len(admins) == 1 and admins[0]["email"] == email

@app.put("/users/{email}/role", dependencies=_RA)
async def users_role(email: str, req: RoleRequest):
    email = email.strip().lower()
    if req.role not in auth.ROLES:
        raise HTTPException(status_code=400, detail="invalid role")
    if await store.user_get(email) is None:
        raise HTTPException(status_code=404, detail="user not found")
    if req.role != "admin" and await _is_last_active_admin(email):
        raise HTTPException(status_code=400, detail="cannot demote the last active admin")
    await store.user_set_role(email, req.role)
    await store.append_audit(auth.actor(), "user_role", email, True, f"role={req.role}")
    return {"ok": True}

@app.delete("/users/{email}", dependencies=_RA)
async def users_deactivate(email: str):
    email = email.strip().lower()
    if await store.user_get(email) is None:
        raise HTTPException(status_code=404, detail="user not found")
    if await _is_last_active_admin(email):
        raise HTTPException(status_code=400, detail="cannot deactivate the last active admin")
    await store.user_set_active(email, False)
    await store.append_audit(auth.actor(), "user_deactivate", email, True)
    return {"ok": True}

class ScaleRequest(BaseModel):
    replicas: int = Field(ge=0, le=100)

class AutoscaleRequest(BaseModel):
    min: int = Field(ge=1, le=100)
    max: int = Field(ge=1, le=100)
    cpu: int = Field(ge=1, le=100)

async def _cluster(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="cluster call timed out")
    except Exception as e:
        msg = str(e)
        if "NotFound" in msg or "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg)   # missing resource = client error
        raise HTTPException(status_code=502, detail=msg)

@app.get("/namespaces", dependencies=_RV)
async def namespaces():
    return {"namespaces": await _cluster(cluster.list_namespaces)}

@app.get("/namespaces/{ns}/workloads", dependencies=_RV)
async def workloads(ns: str):
    return {"workloads": await _cluster(cluster.list_workloads, ns)}

@app.get("/namespaces/{ns}/workloads/{name}", dependencies=_RV)
async def workload_summary(ns: str, name: str):
    return await _cluster(cluster.get_summary, ns, name)

@app.get("/namespaces/{ns}/workloads/{name}/logs", dependencies=_RV)
async def workload_logs(ns: str, name: str, tail: int = 200):
    return {"logs": await _cluster(cluster.get_logs, ns, name, min(max(tail, 1), 2000))}

@app.post("/namespaces/{ns}/workloads/{name}/forward", dependencies=_TG)
async def workload_forward(ns: str, name: str):
    return await _cluster(cluster.forward, ns, name)

@app.post("/namespaces/{ns}/workloads/{name}/forward/stop", dependencies=_TG)
async def workload_forward_stop(ns: str, name: str):
    return await _cluster(cluster.stop_forward, ns, name)

class KeysRequest(BaseModel):
    keys: list[str] = []

@app.post("/forwards/heartbeat", dependencies=_TG)
async def forwards_heartbeat(req: KeysRequest):
    # UI keepalive: mark the forwards it still has open as in-use (reaper spares them)
    for k in req.keys[:100]:
        portforward.touch(k)
    return {"ok": True, "active": portforward.active()}

@app.post("/forwards/stop", dependencies=_TG)
async def forwards_stop(req: KeysRequest):
    # pagehide beacon: stop the forwards the closing window owned
    for k in req.keys[:100]:
        await asyncio.to_thread(portforward.stop, k)
    return {"ok": True}

# ---------- health (unauthenticated — for k8s probes / load balancers) ----------
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}

@app.get("/readyz")
async def readyz():
    ok = await store.healthy()
    if not ok:
        raise HTTPException(status_code=503, detail="store not ready")
    return {"ready": True, "store": store.backend_name()}

# ---------- durable history + audit trail (token-gated) ----------
@app.get("/history", dependencies=_RV)
async def history(limit: int = 200):
    return {"events": await store.recent_events(min(max(limit, 1), 2000))}

@app.get("/audit", dependencies=_RA)
async def audit_log(limit: int = 200):
    return {"audit": await store.recent_audit(min(max(limit, 1), 2000))}

@app.post("/namespaces/{ns}/workloads/{name}/scale", dependencies=_TG)
async def workload_scale(ns: str, name: str, req: ScaleRequest):
    r = await _cluster(cluster.scale, ns, name, req.replicas)
    await store.append_audit(auth.actor(), "scale", f"{ns}/{name}", True, f"replicas={req.replicas}")
    return r

@app.post("/namespaces/{ns}/workloads/{name}/stop", dependencies=_TG)
async def workload_stop(ns: str, name: str):
    r = await _cluster(cluster.stop, ns, name)
    await store.append_audit(auth.actor(), "stop", f"{ns}/{name}", True)
    return r

@app.post("/namespaces/{ns}/workloads/{name}/restart", dependencies=_TG)
async def workload_restart(ns: str, name: str):
    r = await _cluster(cluster.restart, ns, name)
    await store.append_audit(auth.actor(), "restart", f"{ns}/{name}", True)
    return r

@app.post("/namespaces/{ns}/workloads/{name}/autoscale", dependencies=_TG)
async def workload_autoscale(ns: str, name: str, req: AutoscaleRequest):
    r = await _cluster(cluster.set_autoscale, ns, name, req.min, req.max, req.cpu)
    await store.append_audit(auth.actor(), "autoscale", f"{ns}/{name}", True,
                             f"min={req.min} max={req.max} cpu={req.cpu}")
    return r

@app.post("/namespaces/{ns}/workloads/{name}/autoscale/disable", dependencies=_TG)
async def workload_autoscale_off(ns: str, name: str):
    return await _cluster(cluster.disable_autoscale, ns, name)

@app.delete("/namespaces/{ns}/workloads/{name}", dependencies=_RA)
async def workload_delete(ns: str, name: str, confirm: str = ""):
    # two-step confirmation: the client must echo the exact workload name
    if confirm != name:
        raise HTTPException(status_code=400,
                            detail="confirmation required: pass ?confirm=<workload name>")
    r = await _cluster(cluster.delete_app, ns, name)
    await store.append_audit(auth.actor(), "delete", f"{ns}/{name}", True, str(r.get("method", "")))
    return r

@app.get("/manage")
async def manage():
    return FileResponse(os.path.join(STATIC, "manage.html"))

@app.get("/events", dependencies=_RV)
async def events():
    # viewer-gated: the live stream carries the same deploy activity (commands, errors,
    # pod logs) that /history persists — must not be readable unauthenticated.
    # EventSource sends the same-origin session cookie, so the UI keeps working.
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
