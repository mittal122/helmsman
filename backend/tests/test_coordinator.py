import asyncio
import pytest
from events import EventBus
import coordinator
import approvals as approvals_mod
import monitors as monitors_mod

@pytest.mark.asyncio
async def test_happy_path_emits_stages_and_endpoint(monkeypatch):
    monkeypatch.setattr(coordinator.manifests, "render", lambda cfg: "kind: Deployment")
    monkeypatch.setattr(coordinator.validate, "validate", lambda m, ns: (True, []))
    monkeypatch.setattr(coordinator.deploy, "install", lambda cfg: None)
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (2, 2))
    monkeypatch.setattr(coordinator.deploy, "get_endpoint",
                        lambda n, ns, p: {"service": "s", "port": p, "port_forward": "pf"})
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "MONITOR_MAX_CYCLES", 1)

    bus = EventBus()
    q = bus.subscribe()
    mons = monitors_mod.Monitors()
    await coordinator.run({"name": "app", "image": "i:1", "namespace": "default",
                           "port": 8080, "replicas": 2, "mode": "autonomous"},
                          bus, approvals_mod.Approvals(), mons)

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
                           "port": 8080, "replicas": 2}, bus, approvals_mod.Approvals(),
                          monitors_mod.Monitors())

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
                           "port": 8080, "replicas": 2, "mode": "autonomous"},
                          bus, approvals_mod.Approvals(), monitors_mod.Monitors())

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
                           "port": 8080, "replicas": 2}, bus, approvals_mod.Approvals(),
                          monitors_mod.Monitors())

    events = []
    while not q.empty():
        events.append(await q.get())
    types = [e.type for e in events]
    assert "error" in types
    err = next(e for e in events if e.type == "error")
    assert err.stage == "Generate"

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
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "MONITOR_MAX_CYCLES", 1)
    installed = {"called": False}
    monkeypatch.setattr(coordinator.deploy, "install",
                        lambda cfg: installed.__setitem__("called", True))
    bus = EventBus(); q = bus.subscribe(); appr = approvals_mod.Approvals()
    mons = monitors_mod.Monitors()
    task = asyncio.create_task(coordinator.run(_cfg(), bus, appr, mons))
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
    task = asyncio.create_task(coordinator.run(_cfg(), bus, appr, monitors_mod.Monitors()))
    await asyncio.sleep(0.05)
    appr.resolve("app", False)
    await task
    assert installed["called"] is False
    types = [ (await q.get()).type for _ in range(q.qsize()) ]
    assert "rejected" in types

@pytest.mark.asyncio
async def test_autonomous_mode_skips_gate(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "MONITOR_MAX_CYCLES", 1)
    bus = EventBus(); q = bus.subscribe(); appr = approvals_mod.Approvals()
    mons = monitors_mod.Monitors()
    await coordinator.run(_cfg(mode="autonomous"), bus, appr, mons)
    types = [ (await q.get()).type for _ in range(q.qsize()) ]
    assert "endpoint" in types and "approval_required" not in types

@pytest.mark.asyncio
async def test_secret_values_are_redacted_in_events(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.manifests, "render",
                        lambda cfg: "stringData:\n  TOKEN: s3cret")
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "MONITOR_MAX_CYCLES", 1)
    bus = EventBus(); q = bus.subscribe(); appr = approvals_mod.Approvals()
    mons = monitors_mod.Monitors()
    await coordinator.run(_cfg(mode="autonomous", secrets={"TOKEN": "s3cret"}), bus, appr, mons)
    dumped = ""
    while not q.empty():
        dumped += str((await q.get()).to_dict())
    assert "s3cret" not in dumped
    assert "••••" in dumped

@pytest.mark.asyncio
async def test_monitor_stage_emits_health_and_stops(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.monitor, "detect_failures",
                        lambda n, ns: [{"pod": "p", "container": "app",
                                        "type": "CrashLoopBackOff", "message": "x"}])
    monkeypatch.setattr(coordinator.monitor, "get_metrics",
                        lambda n, ns: [{"pod": "p", "cpu": "5m", "memory": "40Mi"}])
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "MONITOR_MAX_CYCLES", 1)
    bus = EventBus(); q = bus.subscribe()
    appr = approvals_mod.Approvals(); mons = monitors_mod.Monitors()
    await coordinator.run(_cfg(mode="autonomous"), bus, appr, mons)
    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "health" in types
    assert "failure" in types      # the injected CrashLoopBackOff surfaced

@pytest.mark.asyncio
async def test_monitor_stops_on_signal(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.monitor, "detect_failures", lambda n, ns: [])
    monkeypatch.setattr(coordinator.monitor, "get_metrics", lambda n, ns: [])
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    # high cap so ONLY the stop signal can end the loop
    monkeypatch.setattr(coordinator, "MONITOR_MAX_CYCLES", 10_000)
    bus = EventBus(); q = bus.subscribe()
    appr = approvals_mod.Approvals(); mons = monitors_mod.Monitors()
    task = asyncio.create_task(coordinator.run(_cfg(mode="autonomous"), bus, appr, mons))
    await asyncio.sleep(0.02)      # let it enter the monitor loop and emit some snapshots
    mons.stop("app")               # signal stop
    await asyncio.wait_for(task, timeout=2)   # must exit promptly, not run 10k cycles
    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert types.count("health") >= 1
    assert "stage_exit" in types   # Monitor stage exited cleanly
