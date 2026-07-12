import asyncio
import pytest
from events import EventBus
import coordinator
import approvals as approvals_mod
import monitors as monitors_mod
import breakers as breakers_mod

@pytest.mark.asyncio
async def test_happy_path_emits_stages_and_endpoint(monkeypatch):
    monkeypatch.setattr(coordinator.deploy, "cluster_reachable", lambda *a, **k: (True, "v1.30"))
    monkeypatch.setattr(coordinator.portforward, "start", lambda *a, **k: 12345)
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
                          bus, approvals_mod.Approvals(), mons, breakers_mod.Breaker())

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
    monkeypatch.setattr(coordinator.deploy, "cluster_reachable", lambda *a, **k: (True, "v1.30"))
    monkeypatch.setattr(coordinator.manifests, "render", lambda cfg: "bad")
    monkeypatch.setattr(coordinator.validate, "validate", lambda m, ns: (False, ["schema: nope"]))
    installed = {"called": False}
    monkeypatch.setattr(coordinator.deploy, "install",
                        lambda cfg: installed.__setitem__("called", True))

    bus = EventBus()
    q = bus.subscribe()
    await coordinator.run({"name": "app", "image": "i:1", "namespace": "default",
                           "port": 8080, "replicas": 2}, bus, approvals_mod.Approvals(),
                          monitors_mod.Monitors(), breakers_mod.Breaker())

    assert installed["called"] is False
    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "error" in types

@pytest.mark.asyncio
async def test_validation_failure_emits_actionable_guidance(monkeypatch):
    # self-healing "guide" rung: a validation break the agent can't auto-fix must
    # still emit clear, actionable guidance (deterministic, even with the LLM down)
    monkeypatch.setattr(coordinator.deploy, "cluster_reachable", lambda *a, **k: (True, "v1.30"))
    monkeypatch.setattr(coordinator.manifests, "render", lambda cfg: "y")
    monkeypatch.setattr(coordinator.validate, "validate",
                        lambda m, ns: (False, ["kube-score: [CRITICAL] (apex) Image with latest tag"]))
    monkeypatch.setattr(coordinator.error_resolver, "resolve",
                        lambda ctx: (_ for _ in ()).throw(RuntimeError("no api key")))  # LLM down
    bus = EventBus()
    q = bus.subscribe()
    await coordinator.run({"name": "apex", "image": "apex", "namespace": "default",
                           "port": 8080, "replicas": 2}, bus, approvals_mod.Approvals(),
                          monitors_mod.Monitors(), breakers_mod.Breaker())
    events = []
    while not q.empty():
        events.append(await q.get())
    g = next(e for e in events if e.type == "guidance")
    assert g.stage == "Validate"
    item = g.data["items"][0]
    assert item["problem"] == "Your image has no pinned version tag"
    assert "version" in item["fix"].lower() and "1.4.2" in item["fix"]

@pytest.mark.asyncio
async def test_rollout_timeout_emits_error(monkeypatch):
    monkeypatch.setattr(coordinator.deploy, "cluster_reachable", lambda *a, **k: (True, "v1.30"))
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
                          bus, approvals_mod.Approvals(), monitors_mod.Monitors(), breakers_mod.Breaker())

    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "error" in types
    assert "endpoint" not in types

@pytest.mark.asyncio
async def test_exception_surfaced_as_error(monkeypatch):
    monkeypatch.setattr(coordinator.deploy, "cluster_reachable", lambda *a, **k: (True, "v1.30"))
    def _boom(cfg):
        raise RuntimeError("boom")
    monkeypatch.setattr(coordinator.manifests, "render", _boom)

    bus = EventBus()
    q = bus.subscribe()
    await coordinator.run({"name": "app", "image": "i:1", "namespace": "default",
                           "port": 8080, "replicas": 2}, bus, approvals_mod.Approvals(),
                          monitors_mod.Monitors(), breakers_mod.Breaker())

    events = []
    while not q.empty():
        events.append(await q.get())
    types = [e.type for e in events]
    assert "error" in types
    err = next(e for e in events if e.type == "error")
    assert err.stage == "Generate"
    # raw detail for the "extract error report" feature: traceback (where the platform
    # code broke) + the command that was running when it failed
    assert err.data.get("kind") == "internal"
    assert "coordinator.py" in err.data["traceback"] and "boom" in err.data["traceback"]
    assert "helm template" in err.data["command"]

@pytest.mark.asyncio
async def test_unreachable_cluster_fails_fast_with_error(monkeypatch):
    # preflight: an unreachable cluster must emit a clear error and never render/deploy
    monkeypatch.setattr(coordinator.deploy, "cluster_reachable",
                        lambda *a, **k: (False, "kubectl timed out"))
    rendered = {"called": False}
    monkeypatch.setattr(coordinator.manifests, "render",
                        lambda cfg: rendered.__setitem__("called", True) or "y")
    bus = EventBus()
    q = bus.subscribe()
    await coordinator.run({"name": "app", "image": "i:1", "namespace": "default",
                           "port": 8080, "replicas": 2}, bus, approvals_mod.Approvals(),
                          monitors_mod.Monitors(), breakers_mod.Breaker())
    assert rendered["called"] is False
    events = []
    while not q.empty():
        events.append(await q.get())
    err = next(e for e in events if e.type == "error")
    assert err.stage == "Detect" and "reach" in err.message.lower()

def _stub_tools(monkeypatch):
    monkeypatch.setattr(coordinator.deploy, "cluster_reachable", lambda *a, **k: (True, "test"))
    monkeypatch.setattr(coordinator.portforward, "start", lambda *a, **k: 12345)
    monkeypatch.setattr(coordinator.portforward, "stop_all", lambda: None)
    monkeypatch.setattr(coordinator.manifests, "render", lambda cfg: "kind: Deployment")
    monkeypatch.setattr(coordinator.validate, "validate", lambda m, ns: (True, []))
    monkeypatch.setattr(coordinator.scan, "scan_image", lambda image, **k: {
        "available": False, "ok": True, "findings": [], "summary": "stub"})
    monkeypatch.setattr(coordinator.scan, "scan_config", lambda manifests: {
        "available": False, "ok": True, "findings": [], "summary": "stub"})
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
    task = asyncio.create_task(coordinator.run(_cfg(), bus, appr, mons, breakers_mod.Breaker()))
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
    task = asyncio.create_task(coordinator.run(_cfg(), bus, appr, monitors_mod.Monitors(), breakers_mod.Breaker()))
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
    await coordinator.run(_cfg(mode="autonomous"), bus, appr, mons, breakers_mod.Breaker())
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
    await coordinator.run(_cfg(mode="autonomous", secrets={"TOKEN": "s3cret"}), bus, appr, mons, breakers_mod.Breaker())
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
    monkeypatch.setattr(coordinator.monitor, "get_logs", lambda n, ns: "")
    monkeypatch.setattr(coordinator.error_resolver, "resolve",
                        lambda ctx: {"root_cause": "", "plain_explanation": "", "evidence": [],
                                     "recommended_action": "", "fix_prompt": "", "auto_remediable": False,
                                     "suggested_auto_action": "", "severity": "low",
                                     "suspicious_input_detected": False})
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "MONITOR_MAX_CYCLES", 1)
    bus = EventBus(); q = bus.subscribe()
    appr = approvals_mod.Approvals(); mons = monitors_mod.Monitors()
    await coordinator.run(_cfg(mode="autonomous"), bus, appr, mons, breakers_mod.Breaker())
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
    task = asyncio.create_task(coordinator.run(_cfg(mode="autonomous"), bus, appr, mons, breakers_mod.Breaker()))
    await asyncio.sleep(0.02)      # let it enter the monitor loop and emit some snapshots
    mons.stop("app")               # signal stop
    await asyncio.wait_for(task, timeout=2)   # must exit promptly, not run 10k cycles
    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert types.count("health") >= 1
    assert "stage_exit" in types   # Monitor stage exited cleanly

@pytest.mark.asyncio
async def test_verify_surfaces_deploy_time_failure(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (0, 1))  # never ready
    monkeypatch.setattr(coordinator.monitor, "detect_failures",
                        lambda n, ns: [{"pod": "broken-x", "container": "app",
                                        "type": "ImagePullBackOff", "message": "no such image"}])
    monkeypatch.setattr(coordinator, "ROLLOUT_TIMEOUT_S", 0)  # loop body runs 0 times -> straight to timeout else
    bus = EventBus(); q = bus.subscribe()
    appr = approvals_mod.Approvals(); mons = monitors_mod.Monitors()
    await coordinator.run(_cfg(mode="autonomous"), bus, appr, mons, breakers_mod.Breaker())
    events = []
    while not q.empty():
        events.append(await q.get())
    err = [e for e in events if e.type == "error"]
    assert err and err[0].data.get("failures")   # timeout error carries the detected failure
    assert err[0].data["failures"][0]["type"] == "ImagePullBackOff"

@pytest.mark.asyncio
async def test_verify_emits_failure_event_during_rollout(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (0, 1))
    monkeypatch.setattr(coordinator.monitor, "detect_failures",
                        lambda n, ns: [{"pod": "broken-x", "container": "app",
                                        "type": "ImagePullBackOff", "message": "x"}])
    monkeypatch.setattr(coordinator.monitor, "get_logs", lambda n, ns: "")
    monkeypatch.setattr(coordinator.error_resolver, "resolve",
                        lambda ctx: {"root_cause": "", "plain_explanation": "", "evidence": [],
                                     "recommended_action": "", "fix_prompt": "", "auto_remediable": False,
                                     "suggested_auto_action": "", "severity": "low",
                                     "suspicious_input_detected": False})
    monkeypatch.setattr(coordinator, "ROLLOUT_TIMEOUT_S", 2)  # ~1 loop iteration (POLL_INTERVAL_S default 2)

    async def _no_sleep(x):
        pass
    monkeypatch.setattr(coordinator.asyncio, "sleep", _no_sleep)

    bus = EventBus(); q = bus.subscribe()
    appr = approvals_mod.Approvals(); mons = monitors_mod.Monitors()
    await coordinator.run(_cfg(mode="autonomous"), bus, appr, mons, breakers_mod.Breaker())
    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "failure" in types   # surfaced during Verify, not just at timeout

@pytest.mark.asyncio
async def test_monitor_failure_deduped_across_cycles(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.monitor, "detect_failures",
                        lambda n, ns: [{"pod": "p", "container": "app",
                                        "type": "CrashLoopBackOff", "message": "x"}])
    monkeypatch.setattr(coordinator.monitor, "get_metrics", lambda n, ns: [])
    monkeypatch.setattr(coordinator.monitor, "get_logs", lambda n, ns: "")
    monkeypatch.setattr(coordinator.error_resolver, "resolve",
                        lambda ctx: {"root_cause": "", "plain_explanation": "", "evidence": [],
                                     "recommended_action": "", "fix_prompt": "", "auto_remediable": False,
                                     "suggested_auto_action": "", "severity": "low",
                                     "suspicious_input_detected": False})
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "MONITOR_MAX_CYCLES", 3)   # 3 cycles, same failure
    bus = EventBus(); q = bus.subscribe()
    appr = approvals_mod.Approvals(); mons = monitors_mod.Monitors()
    await coordinator.run(_cfg(mode="autonomous"), bus, appr, mons, breakers_mod.Breaker())
    events = []
    while not q.empty():
        events.append(await q.get())
    monitor_failures = [e for e in events if e.type == "failure" and e.stage == "Monitor"]
    monitor_health = [e for e in events if e.type == "health" and e.stage == "Monitor"]
    assert len(monitor_failures) == 1     # emitted once despite 3 cycles of the same failure
    assert len(monitor_health) == 3      # health still every cycle

import agents.error_resolver as error_resolver_mod

@pytest.mark.asyncio
async def test_failure_triggers_explanation(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (0, 1))
    monkeypatch.setattr(coordinator.monitor, "detect_failures",
                        lambda n, ns: [{"pod": "p", "container": "app",
                                        "type": "ImagePullBackOff", "message": "no image"}])
    monkeypatch.setattr(coordinator.monitor, "get_logs", lambda n, ns: "log line")
    monkeypatch.setattr(coordinator.error_resolver, "resolve",
                        lambda ctx: {"root_cause": "bad image tag", "plain_explanation": "x",
                                     "evidence": [], "recommended_action": "fix tag",
                                     "fix_prompt": "", "auto_remediable": False,
                                     "suggested_auto_action": "", "severity": "high",
                                     "suspicious_input_detected": False})
    monkeypatch.setattr(coordinator, "POLL_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "ROLLOUT_TIMEOUT_S", 2)
    async def _no_sleep(x): pass
    monkeypatch.setattr(coordinator.asyncio, "sleep", _no_sleep)
    bus = EventBus(); q = bus.subscribe()
    appr = approvals_mod.Approvals(); mons = monitors_mod.Monitors()
    await coordinator.run(_cfg(mode="autonomous"), bus, appr, mons, breakers_mod.Breaker())
    events = []
    while not q.empty():
        events.append(await q.get())
    # a runtime pod failure produces actionable guidance (deterministic + LLM enrichment)
    g = next(e for e in events if e.type == "guidance")
    assert "ImagePullBackOff" in g.data["items"][0]["problem"]
    assert g.data.get("ai", {}).get("root_cause") == "bad image tag"  # LLM enrichment folded in

@pytest.mark.asyncio
async def test_explanation_failure_does_not_crash(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (0, 1))
    monkeypatch.setattr(coordinator.monitor, "detect_failures",
                        lambda n, ns: [{"pod": "p", "container": "app",
                                        "type": "ImagePullBackOff", "message": "x"}])
    monkeypatch.setattr(coordinator.monitor, "get_logs", lambda n, ns: "")
    def _boom(ctx): raise RuntimeError("api down")
    monkeypatch.setattr(coordinator.error_resolver, "resolve", _boom)
    monkeypatch.setattr(coordinator, "POLL_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "ROLLOUT_TIMEOUT_S", 2)
    async def _no_sleep(x): pass
    monkeypatch.setattr(coordinator.asyncio, "sleep", _no_sleep)
    bus = EventBus(); q = bus.subscribe()
    appr = approvals_mod.Approvals(); mons = monitors_mod.Monitors()
    await coordinator.run(_cfg(mode="autonomous"), bus, appr, mons, breakers_mod.Breaker())  # must not raise
    events = []
    while not q.empty():
        events.append(await q.get())
    types = [e.type for e in events]
    assert "error" in types  # rollout still times out; no crash
    # BUG FIX: LLM down (no API key) must NOT leak the raw SDK auth error into the feed…
    assert not any("AI explanation unavailable" in (e.message or "") for e in events)
    # …and the user still gets deterministic guidance for the pod failure
    assert "guidance" in types

def _cfg_auto(**over):
    base = {"name": "app", "image": "i:1", "namespace": "default", "port": 8080,
            "replicas": 1, "mode": "autonomous", "secrets": {}}
    base.update(over); return base

@pytest.mark.asyncio
async def test_autonomous_rollback_on_failure(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (0, 1))  # never ready
    monkeypatch.setattr(coordinator.monitor, "detect_failures", lambda n, ns: [])
    monkeypatch.setattr(coordinator.monitor, "get_logs", lambda n, ns: "")
    monkeypatch.setattr(coordinator.error_resolver, "resolve", lambda ctx: {
        "root_cause": "", "plain_explanation": "", "evidence": [], "recommended_action": "",
        "fix_prompt": "", "auto_remediable": False, "suggested_auto_action": "",
        "severity": "low", "suspicious_input_detected": False})
    monkeypatch.setattr(coordinator.rollback, "get_revisions",
                        lambda n, ns: [{"revision": 1, "status": "superseded"},
                                       {"revision": 2, "status": "deployed"}])
    rolled = {}
    monkeypatch.setattr(coordinator.rollback, "do_rollback",
                        lambda n, ns, rev: rolled.update(rev=rev))
    monkeypatch.setattr(coordinator, "POLL_INTERVAL_S", 2)
    monkeypatch.setattr(coordinator, "ROLLOUT_TIMEOUT_S", 2)
    async def _no_sleep(x): pass
    monkeypatch.setattr(coordinator.asyncio, "sleep", _no_sleep)
    bus = EventBus(); q = bus.subscribe()
    appr = approvals_mod.Approvals(); mons = monitors_mod.Monitors(); brk = breakers_mod.Breaker(max_attempts=2)
    await coordinator.run(_cfg_auto(), bus, appr, mons, brk)
    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "remediation" in types
    assert rolled.get("rev") == 1     # rolled back to the prior good revision

@pytest.mark.asyncio
async def test_no_prior_revision_escalates(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (0, 1))
    monkeypatch.setattr(coordinator.monitor, "detect_failures", lambda n, ns: [])
    monkeypatch.setattr(coordinator.monitor, "get_logs", lambda n, ns: "")
    monkeypatch.setattr(coordinator.error_resolver, "resolve", lambda ctx: {
        "root_cause": "", "plain_explanation": "", "evidence": [], "recommended_action": "",
        "fix_prompt": "", "auto_remediable": False, "suggested_auto_action": "",
        "severity": "low", "suspicious_input_detected": False})
    monkeypatch.setattr(coordinator.rollback, "get_revisions",
                        lambda n, ns: [{"revision": 1, "status": "deployed"}])  # first deploy, nothing prior
    called = {"rb": False}
    monkeypatch.setattr(coordinator.rollback, "do_rollback",
                        lambda n, ns, rev: called.__setitem__("rb", True))
    monkeypatch.setattr(coordinator, "POLL_INTERVAL_S", 2)
    monkeypatch.setattr(coordinator, "ROLLOUT_TIMEOUT_S", 2)
    async def _no_sleep(x): pass
    monkeypatch.setattr(coordinator.asyncio, "sleep", _no_sleep)
    bus = EventBus(); q = bus.subscribe()
    appr = approvals_mod.Approvals(); mons = monitors_mod.Monitors(); brk = breakers_mod.Breaker()
    await coordinator.run(_cfg_auto(), bus, appr, mons, brk)
    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "escalation" in types
    assert called["rb"] is False

@pytest.mark.asyncio
async def test_breaker_tripped_freezes(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (0, 1))
    monkeypatch.setattr(coordinator.monitor, "detect_failures", lambda n, ns: [])
    monkeypatch.setattr(coordinator.monitor, "get_logs", lambda n, ns: "")
    monkeypatch.setattr(coordinator.error_resolver, "resolve", lambda ctx: {
        "root_cause": "", "plain_explanation": "", "evidence": [], "recommended_action": "",
        "fix_prompt": "", "auto_remediable": False, "suggested_auto_action": "",
        "severity": "low", "suspicious_input_detected": False})
    monkeypatch.setattr(coordinator.rollback, "get_revisions",
                        lambda n, ns: [{"revision": 1, "status": "superseded"},
                                       {"revision": 2, "status": "deployed"}])
    monkeypatch.setattr(coordinator.rollback, "do_rollback", lambda n, ns, rev: None)
    monkeypatch.setattr(coordinator, "POLL_INTERVAL_S", 2)
    monkeypatch.setattr(coordinator, "ROLLOUT_TIMEOUT_S", 2)
    async def _no_sleep(x): pass
    monkeypatch.setattr(coordinator.asyncio, "sleep", _no_sleep)
    brk = breakers_mod.Breaker(max_attempts=1)
    brk.record("app")   # already at the limit
    bus = EventBus(); q = bus.subscribe()
    appr = approvals_mod.Approvals(); mons = monitors_mod.Monitors()
    await coordinator.run(_cfg_auto(), bus, appr, mons, brk)
    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "escalation" in types  # frozen by the breaker

def test_cluster_selection_sets_and_cleans_kubeconfig(monkeypatch, tmp_path):
    import coordinator, kubeconfig_store, os as _os
    fake = str(tmp_path / "decrypted.kubeconfig")
    open(fake, "w").write("x")
    seen = {}
    monkeypatch.setattr(coordinator.deploy, "cluster_reachable", lambda *a, **k: (True, "test"))
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

@pytest.mark.asyncio
async def test_scan_gate_blocks_deploy_on_critical_finding(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.scan, "scan_image", lambda image, **k: {
        "available": True, "ok": False,
        "findings": [{"id": "CVE-1", "severity": "CRITICAL", "pkg": "openssl", "title": "bad"}],
        "summary": "1 vuln(s) at/above CRITICAL"})
    monkeypatch.setattr(coordinator.scan, "scan_config", lambda manifests: {
        "available": True, "ok": True, "findings": [], "summary": "0 misconfig(s) (advisory)"})
    installed = {"called": False}
    monkeypatch.setattr(coordinator.deploy, "install",
                        lambda cfg: installed.__setitem__("called", True))
    bus = EventBus(); q = bus.subscribe()
    await coordinator.run(_cfg_auto(), bus, approvals_mod.Approvals(),
                          monitors_mod.Monitors(), breakers_mod.Breaker())
    events = []
    while not q.empty():
        events.append(await q.get())
    types = [e.type for e in events]
    assert installed["called"] is False   # gate blocked Deploy
    assert "scan" in types
    assert "error" in types
    err = next(e for e in events if e.type == "error")
    assert err.stage == "Scan"
    assert "endpoint" not in types

@pytest.mark.asyncio
async def test_unknown_cluster_emits_error_not_raise(monkeypatch):
    def _boom(name):
        raise KeyError("nope")
    monkeypatch.setattr(coordinator.kubeconfig_store, "decrypt_to_tempfile", _boom)

    bus = EventBus()
    q = bus.subscribe()
    # must not raise out of run() even though /deploy calls it via fire-and-forget create_task
    await coordinator.run({"name": "app", "image": "i:1", "namespace": "default",
                           "port": 8080, "replicas": 2, "cluster": "unknown"},
                          bus, approvals_mod.Approvals(), monitors_mod.Monitors(), breakers_mod.Breaker())

    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "error" in types

@pytest.mark.asyncio
async def test_successful_rollout_resets_breaker(monkeypatch):
    # a healthy rollout must clear the breaker so it counts CONSECUTIVE failed
    # remediations, not lifetime ones (else self-healing freezes permanently)
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "MONITOR_MAX_CYCLES", 1)
    brk = breakers_mod.Breaker(max_attempts=2)
    brk.record("app")                       # one prior failed remediation on this name
    assert brk.tripped("app") is False
    bus = EventBus()
    await coordinator.run(_cfg(mode="autonomous"), bus, approvals_mod.Approvals(),
                          monitors_mod.Monitors(), brk)
    assert brk.tripped("app") is False      # still fine, and…
    brk.record("app"); assert brk.tripped("app") is False   # …count was reset (needs 2 fresh to trip)

@pytest.mark.asyncio
async def test_approval_resolved_during_emit_is_not_dropped(monkeypatch):
    # race regression: a fast POST /approve landing during approval_required's emit
    # awaits must not be dropped (Future is now registered BEFORE the emit)
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "MONITOR_MAX_CYCLES", 1)
    appr = approvals_mod.Approvals()
    async def racing_append(ev):            # simulate the client resolving mid-emit
        if ev.get("type") == "approval_required":
            appr.resolve("app", True)
    monkeypatch.setattr(coordinator.store, "append_event", racing_append)
    installed = {"c": False}
    monkeypatch.setattr(coordinator.deploy, "install",
                        lambda cfg: installed.__setitem__("c", True))
    bus = EventBus()
    # must COMPLETE (not hang on an unresolved Future) and proceed to deploy
    await asyncio.wait_for(
        coordinator.run(_cfg(mode="manual"), bus, appr, monitors_mod.Monitors(), breakers_mod.Breaker()),
        timeout=3)
    assert installed["c"] is True

@pytest.mark.asyncio
async def test_git_repo_triggers_build_and_flows_tag_into_pipeline(monkeypatch):
    # deploy-from-source: clone + build + load, then the built tag flows into render/deploy
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.builder, "clone", lambda repo, br, ref: ("/tmp/wd", "abc1234"))
    monkeypatch.setattr(coordinator.builder, "image_tag", lambda name, sha: f"{name}:src-{sha}")
    monkeypatch.setattr(coordinator.builder, "build", lambda wd, tag, df: None)
    monkeypatch.setattr(coordinator.builder, "current_context", lambda: "kind-helmsman")
    monkeypatch.setattr(coordinator.builder, "make_available", lambda tag, ctx: "kind")
    cleaned = {}
    monkeypatch.setattr(coordinator.builder, "cleanup", lambda wd: cleaned.__setitem__("wd", wd))
    rendered = {}
    monkeypatch.setattr(coordinator.manifests, "render",
                        lambda cfg: rendered.__setitem__("image", cfg.get("image")) or "kind: Deployment")
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "MONITOR_MAX_CYCLES", 1)
    bus = EventBus(); q = bus.subscribe()
    await coordinator.run(_cfg(mode="autonomous", image="", git_repo="https://github.com/x/y.git",
                               git_branch="main", dockerfile="Dockerfile"),
                          bus, approvals_mod.Approvals(), monitors_mod.Monitors(), breakers_mod.Breaker())
    events = []
    while not q.empty():
        events.append(await q.get())
    pairs = [(e.type, e.stage) for e in events]
    assert ("stage_enter", "Build") in pairs and ("stage_exit", "Build") in pairs
    assert rendered["image"] == "app:src-abc1234"   # built tag drives the deploy
    assert cleaned["wd"] == "/tmp/wd"               # temp clone cleaned up
    assert "endpoint" in [e.type for e in events]

def _compose_cfg(**over):
    base = {"name": "shop", "namespace": "default", "mode": "manual", "cluster": "",
            "warnings": ["custom networks ignored"],
            "services": [
                {"name": "db", "image": "postgres:16", "port": 5432, "replicas": 1,
                 "env": {}, "secrets": {"POSTGRES_PASSWORD": "pw"}, "published": False,
                 "probe": {"type": "tcp"}, "volumes": [], "run_as_user": 999},
                {"name": "web", "image": "nginx:1", "port": 80, "replicas": 1,
                 "env": {}, "secrets": {}, "published": True,
                 "probe": {"type": "tcp"}, "volumes": [], "run_as_user": None},
            ]}
    base.update(over); return base

@pytest.mark.asyncio
async def test_compose_deploys_each_service_with_one_approval(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "MONITOR_MAX_CYCLES", 1)
    installed = []
    monkeypatch.setattr(coordinator.deploy, "install", lambda cfg: installed.append(cfg["name"]))
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (1, 1))
    bus = EventBus(); q = bus.subscribe()
    await coordinator.run(_compose_cfg(mode="autonomous"), bus, approvals_mod.Approvals(),
                          monitors_mod.Monitors(), breakers_mod.Breaker())
    events = []
    while not q.empty():
        events.append(await q.get())
    # both services installed, in depends_on order (db before web)
    assert installed == ["db", "web"]
    stages = {e.stage for e in events}
    assert "db:Deploy" in stages and "web:Deploy" in stages
    # exactly one endpoint (only web is published)
    eps = [e for e in events if e.type == "endpoint"]
    assert len(eps) == 1 and eps[0].data["service_name"] == "web"
    # warnings surfaced
    assert any("networks" in e.message for e in events if e.type == "info")

@pytest.mark.asyncio
async def test_compose_one_approval_gate_blocks_all(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "MONITOR_MAX_CYCLES", 1)
    installed = []
    monkeypatch.setattr(coordinator.deploy, "install", lambda cfg: installed.append(cfg["name"]))
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (1, 1))
    appr = approvals_mod.Approvals()
    bus = EventBus(); q = bus.subscribe()
    task = asyncio.create_task(coordinator.run(_compose_cfg(), bus, appr,
                               monitors_mod.Monitors(), breakers_mod.Breaker()))
    await asyncio.sleep(0.05)
    assert installed == []                     # nothing deployed pending the single gate
    assert appr.resolve("shop", True) is True  # gate keyed by STACK name
    await task
    assert installed == ["db", "web"]

@pytest.mark.asyncio
async def test_compose_redacts_any_services_secret(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "MONITOR_MAX_CYCLES", 1)
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: (1, 1))
    monkeypatch.setattr(coordinator.manifests, "render",
                        lambda cfg: "stringData:\n  POSTGRES_PASSWORD: s3cr3tpw")
    bus = EventBus(); q = bus.subscribe()
    cfg = _compose_cfg(mode="autonomous")
    cfg["services"][0]["secrets"] = {"POSTGRES_PASSWORD": "s3cr3tpw"}
    await coordinator.run(cfg, bus, approvals_mod.Approvals(),
                          monitors_mod.Monitors(), breakers_mod.Breaker())
    dumped = ""
    while not q.empty():
        dumped += str((await q.get()).to_dict())
    assert "s3cr3tpw" not in dumped and "••••" in dumped   # any service's secret redacted everywhere

@pytest.mark.asyncio
async def test_build_autodetects_sole_nonstandard_dockerfile(monkeypatch):
    # no dockerfile given, no root Dockerfile, exactly one match -> auto-use it
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.builder, "clone", lambda repo, br, ref: ("/tmp/wd", "sha1"))
    monkeypatch.setattr(coordinator.builder, "list_dockerfiles", lambda wd: ["Dockerfile.prod"])
    monkeypatch.setattr(coordinator.builder, "image_tag", lambda name, sha: f"{name}:src-{sha}")
    monkeypatch.setattr(coordinator.builder, "current_context", lambda: "kind-helmsman")
    monkeypatch.setattr(coordinator.builder, "make_available", lambda tag, ctx: "kind")
    monkeypatch.setattr(coordinator.builder, "cleanup", lambda wd: None)
    used = {}
    monkeypatch.setattr(coordinator.builder, "build", lambda wd, tag, df: used.__setitem__("df", df))
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "MONITOR_MAX_CYCLES", 1)
    bus = EventBus(); q = bus.subscribe()
    await coordinator.run(_cfg(mode="autonomous", image="", git_repo="https://github.com/x/y.git", dockerfile=""),
                          bus, approvals_mod.Approvals(), monitors_mod.Monitors(), breakers_mod.Breaker())
    events = []
    while not q.empty():
        events.append(await q.get())
    assert used["df"] == "Dockerfile.prod"
    assert any(e.type == "info" and "Auto-detected" in e.message for e in events)
    assert "endpoint" in [e.type for e in events]

@pytest.mark.asyncio
async def test_build_multiple_dockerfiles_stops_with_list(monkeypatch):
    # no dockerfile given, several matches, no root -> stop and list them, never build/deploy
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.builder, "clone", lambda repo, br, ref: ("/tmp/wd", "sha1"))
    monkeypatch.setattr(coordinator.builder, "list_dockerfiles",
                        lambda wd: ["api/Dockerfile", "web/Dockerfile"])
    monkeypatch.setattr(coordinator.builder, "cleanup", lambda wd: None)
    built = {"c": False}; installed = {"c": False}
    monkeypatch.setattr(coordinator.builder, "build", lambda *a: built.__setitem__("c", True))
    monkeypatch.setattr(coordinator.deploy, "install", lambda cfg: installed.__setitem__("c", True))
    bus = EventBus(); q = bus.subscribe()
    await coordinator.run(_cfg(mode="autonomous", image="", git_repo="https://github.com/x/y.git", dockerfile=""),
                          bus, approvals_mod.Approvals(), monitors_mod.Monitors(), breakers_mod.Breaker())
    events = []
    while not q.empty():
        events.append(await q.get())
    assert built["c"] is False and installed["c"] is False
    err = next(e for e in events if e.type == "error")
    assert err.stage == "Build" and "Multiple Dockerfiles" in err.message
    assert err.data["dockerfiles"] == ["api/Dockerfile", "web/Dockerfile"]

@pytest.mark.asyncio
async def test_build_failure_stops_before_deploy_with_guidance(monkeypatch):
    _stub_tools(monkeypatch)
    monkeypatch.setattr(coordinator.builder, "clone", lambda *a: ("/tmp/wd", "sha"))
    monkeypatch.setattr(coordinator.builder, "build",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("Dockerfile not found in repo: Dockerfile")))
    cleaned = {}
    monkeypatch.setattr(coordinator.builder, "cleanup", lambda wd: cleaned.__setitem__("wd", wd))
    installed = {"c": False}
    monkeypatch.setattr(coordinator.deploy, "install", lambda cfg: installed.__setitem__("c", True))
    bus = EventBus(); q = bus.subscribe()
    await coordinator.run(_cfg(mode="autonomous", image="", git_repo="https://github.com/x/y.git"),
                          bus, approvals_mod.Approvals(), monitors_mod.Monitors(), breakers_mod.Breaker())
    events = []
    while not q.empty():
        events.append(await q.get())
    types = [e.type for e in events]
    assert installed["c"] is False              # build failed -> never deployed
    assert "error" in types and "guidance" in types
    assert next(e for e in events if e.type == "error").stage == "Build"
    g = next(e for e in events if e.type == "guidance")
    assert g.data["items"][0]["problem"] == "The repo has no Dockerfile at that path"
    assert cleaned["wd"] == "/tmp/wd"           # cleanup ran even on failure

@pytest.mark.asyncio
async def test_transient_crash_recovers_no_blocking_guidance(monkeypatch):
    # a pod crash-loops during startup then recovers -> WARN only + "recovered" note +
    # a genuinely-live endpoint with a clickable URL, and NO blocking guidance.
    _stub_tools(monkeypatch)
    reps = iter([(0, 3), (3, 3)])
    monkeypatch.setattr(coordinator.deploy, "get_replicas", lambda n, ns: next(reps, (3, 3)))
    fails = iter([[{"pod": "p", "container": "c", "type": "CrashLoopBackOff", "message": "boom"}], []])
    monkeypatch.setattr(coordinator.monitor, "detect_failures", lambda n, ns: next(fails, []))
    monkeypatch.setattr(coordinator.monitor, "get_metrics", lambda n, ns: [])
    monkeypatch.setattr(coordinator, "POLL_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "MONITOR_INTERVAL_S", 0)
    monkeypatch.setattr(coordinator, "MONITOR_MAX_CYCLES", 1)
    async def _ns(x): pass
    monkeypatch.setattr(coordinator.asyncio, "sleep", _ns)
    bus = EventBus(); q = bus.subscribe(); mons = monitors_mod.Monitors()
    await coordinator.run(_cfg(mode="autonomous", replicas=3), bus,
                          approvals_mod.Approvals(), mons, breakers_mod.Breaker())
    events = []
    while not q.empty():
        events.append(await q.get())
    types = [e.type for e in events]
    assert "failure" in types          # transient crash surfaced as a warning
    assert "guidance" not in types     # but NOT blocking guidance — it recovered
    assert "endpoint" in types
    ep = next(e for e in events if e.type == "endpoint")
    assert ep.data.get("url", "").startswith("http://127.0.0.1:")   # clickable URL
    assert any("recovered" in (e.message or "") for e in events)
