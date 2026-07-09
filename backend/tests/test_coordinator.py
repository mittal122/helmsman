import asyncio
import pytest
from events import EventBus
import coordinator
import approvals as approvals_mod
import monitors as monitors_mod
import breakers as breakers_mod

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
                          bus, approvals_mod.Approvals(), monitors_mod.Monitors(), breakers_mod.Breaker())

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
                          monitors_mod.Monitors(), breakers_mod.Breaker())

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
    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "explanation" in types

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
    types = []
    while not q.empty():
        types.append((await q.get()).type)
    assert "error" in types  # rollout still times out; no crash

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
