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

    events = []
    while not q.empty():
        events.append(await q.get())
    types = [e.type for e in events]
    assert "stage_enter" in types
    assert "endpoint" in types
    assert "error" not in types
    pairs = [(e.type, e.stage) for e in events]
    assert ("stage_exit", "Deploy") in pairs

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

@pytest.mark.asyncio
async def test_rollout_timeout_emits_error(monkeypatch):
    monkeypatch.setattr(coordinator.manifests, "render", lambda cfg: "y")
    monkeypatch.setattr(coordinator.validate, "validate", lambda m, ns: (True, []))
    monkeypatch.setattr(coordinator.deploy, "install", lambda cfg: None)
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (1, 2))
    monkeypatch.setattr(coordinator, "ROLLOUT_TIMEOUT_S", 2)

    async def _no_sleep(x):
        pass
    monkeypatch.setattr(coordinator.asyncio, "sleep", _no_sleep)

    bus = EventBus()
    q = bus.subscribe()
    await coordinator.run({"name": "app", "image": "i:1", "namespace": "default",
                           "port": 8080, "replicas": 2}, bus)

    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "error" in types
    assert "endpoint" not in types

@pytest.mark.asyncio
async def test_exception_surfaced_as_error(monkeypatch):
    def _boom(cfg):
        raise RuntimeError("boom")
    monkeypatch.setattr(coordinator.manifests, "render", _boom)

    bus = EventBus()
    q = bus.subscribe()
    await coordinator.run({"name": "app", "image": "i:1", "namespace": "default",
                           "port": 8080, "replicas": 2}, bus)

    events = []
    while not q.empty():
        events.append(await q.get())
    types = [e.type for e in events]
    assert "error" in types
    err = next(e for e in events if e.type == "error")
    assert err.stage == "Generate"
