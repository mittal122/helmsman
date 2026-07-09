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
_bg_tasks: set = set()
STATIC = os.path.join(os.path.dirname(__file__), "static")

class DeployRequest(BaseModel):
    name: str
    image: str
    namespace: str = "default"
    port: int = 8080
    replicas: int = 2

@app.post("/deploy")
async def deploy(req: DeployRequest):
    task = asyncio.create_task(coordinator_run(req.model_dump(), bus))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
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
