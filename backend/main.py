import asyncio
import json
import os
import re
import subprocess
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, field_validator, Field
import auth
import kubeconfig_store
from events import Event, EventBus
from coordinator import run as coordinator_run
from approvals import Approvals
from monitors import Monitors
from agents import onboarding, config_advisor
from tools import rollback, cluster
from breakers import Breaker

app = FastAPI(title="Helmsman")
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
    cluster: str = ""      # named kubeconfig from the store; "" = ambient (kind)

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

@app.post("/deploy", dependencies=[Depends(auth.require_token)])
async def deploy(req: DeployRequest):
    task = asyncio.create_task(coordinator_run(req.model_dump(), bus, approvals, monitors, breakers))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"deployment_id": req.name}

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

@app.post("/kubeconfigs", dependencies=[Depends(auth.require_token)])
async def add_kubeconfig(req: KubeconfigRequest):
    kubeconfig_store.save(req.name, req.content.encode())
    return {"ok": True}

@app.get("/kubeconfigs", dependencies=[Depends(auth.require_token)])
async def list_kubeconfigs():
    return {"names": kubeconfig_store.list_names()}

@app.delete("/kubeconfigs/{name}", dependencies=[Depends(auth.require_token)])
async def delete_kubeconfig(name: str):
    try:
        valid = _dns1123(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": kubeconfig_store.delete(valid)}

# ---------- Cluster management (SRE console) — token-gated, error-mapped ----------
_TG = [Depends(auth.require_token)]

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
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/namespaces", dependencies=_TG)
async def namespaces():
    return {"namespaces": await _cluster(cluster.list_namespaces)}

@app.get("/namespaces/{ns}/workloads", dependencies=_TG)
async def workloads(ns: str):
    return {"workloads": await _cluster(cluster.list_workloads, ns)}

@app.get("/namespaces/{ns}/workloads/{name}", dependencies=_TG)
async def workload_summary(ns: str, name: str):
    return await _cluster(cluster.get_summary, ns, name)

@app.get("/namespaces/{ns}/workloads/{name}/logs", dependencies=_TG)
async def workload_logs(ns: str, name: str, tail: int = 200):
    return {"logs": await _cluster(cluster.get_logs, ns, name, min(max(tail, 1), 2000))}

@app.post("/namespaces/{ns}/workloads/{name}/forward", dependencies=_TG)
async def workload_forward(ns: str, name: str):
    return await _cluster(cluster.forward, ns, name)

@app.post("/namespaces/{ns}/workloads/{name}/forward/stop", dependencies=_TG)
async def workload_forward_stop(ns: str, name: str):
    return await _cluster(cluster.stop_forward, ns, name)

@app.post("/namespaces/{ns}/workloads/{name}/scale", dependencies=_TG)
async def workload_scale(ns: str, name: str, req: ScaleRequest):
    return await _cluster(cluster.scale, ns, name, req.replicas)

@app.post("/namespaces/{ns}/workloads/{name}/stop", dependencies=_TG)
async def workload_stop(ns: str, name: str):
    return await _cluster(cluster.stop, ns, name)

@app.post("/namespaces/{ns}/workloads/{name}/restart", dependencies=_TG)
async def workload_restart(ns: str, name: str):
    return await _cluster(cluster.restart, ns, name)

@app.post("/namespaces/{ns}/workloads/{name}/autoscale", dependencies=_TG)
async def workload_autoscale(ns: str, name: str, req: AutoscaleRequest):
    return await _cluster(cluster.set_autoscale, ns, name, req.min, req.max, req.cpu)

@app.post("/namespaces/{ns}/workloads/{name}/autoscale/disable", dependencies=_TG)
async def workload_autoscale_off(ns: str, name: str):
    return await _cluster(cluster.disable_autoscale, ns, name)

@app.delete("/namespaces/{ns}/workloads/{name}", dependencies=_TG)
async def workload_delete(ns: str, name: str, confirm: str = ""):
    # two-step confirmation: the client must echo the exact workload name
    if confirm != name:
        raise HTTPException(status_code=400,
                            detail="confirmation required: pass ?confirm=<workload name>")
    return await _cluster(cluster.delete_app, ns, name)

@app.get("/manage")
async def manage():
    return FileResponse(os.path.join(STATIC, "manage.html"))

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
