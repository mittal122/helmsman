import asyncio
import json
import os
import re
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, field_validator
from events import EventBus
from coordinator import run as coordinator_run
from approvals import Approvals
from monitors import Monitors
from agents import onboarding, config_advisor

app = FastAPI(title="Helmsman")
bus = EventBus()
approvals = Approvals()
monitors = Monitors()
_bg_tasks: set = set()
STATIC = os.path.join(os.path.dirname(__file__), "static")

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

class MonitorStopRequest(BaseModel):
    name: str

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

@app.post("/deploy")
async def deploy(req: DeployRequest):
    task = asyncio.create_task(coordinator_run(req.model_dump(), bus, approvals, monitors))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"deployment_id": req.name}

@app.post("/approve")
async def approve(req: ApproveRequest):
    return {"ok": approvals.resolve(req.name, req.approved)}

@app.post("/monitor/stop")
async def monitor_stop(req: MonitorStopRequest):
    monitors.stop(req.name)
    return {"ok": True}

@app.post("/advise-config")
async def advise_config(req: AdviseRequest):
    return await asyncio.to_thread(config_advisor.advise, req.model_dump())

@app.post("/onboard")
async def onboard(req: OnboardRequest):
    return await asyncio.to_thread(onboarding.generate, req.model_dump())

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
