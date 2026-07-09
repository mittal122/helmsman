import asyncio
from events import Event, EventBus
from tools import manifests, validate, deploy, monitor
from approvals import Approvals
from monitors import Monitors
import guardrails

ROLLOUT_TIMEOUT_S = 120
POLL_INTERVAL_S = 2
MONITOR_INTERVAL_S = 5
MONITOR_MAX_CYCLES = 720   # safety cap (~1h at 5s); real stop is the Monitors flag

async def run(cfg: dict, bus: EventBus, approvals: Approvals, monitors: Monitors) -> None:
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
        seen_failures: set = set()
        for _ in range(ROLLOUT_TIMEOUT_S // POLL_INTERVAL_S):
            ready, desired = await asyncio.to_thread(deploy.get_replicas, name, ns)
            if (ready, desired) != last:
                await emit("rollout", "Verify", f"{ready}/{desired} ready",
                           {"ready": ready, "desired": desired})
                last = (ready, desired)
            for f in await asyncio.to_thread(monitor.detect_failures, name, ns):
                key = (f["pod"], f["type"])
                if key not in seen_failures:
                    seen_failures.add(key)
                    await emit("failure", "Verify", f"{f['type']} on {f['pod']}", f)
            if desired and ready >= desired:
                break
            await asyncio.sleep(POLL_INTERVAL_S)
        else:
            failures = await asyncio.to_thread(monitor.detect_failures, name, ns)
            await emit("error", "Verify", "Rollout did not complete in time",
                       {"timeout_s": ROLLOUT_TIMEOUT_S, "failures": failures})
            return

        ep = await asyncio.to_thread(deploy.get_endpoint, name, ns, port)
        await emit("endpoint", "Verify", "Deployment is live", ep)
        await emit("stage_exit", "Verify", "Done")

        # Monitor (continuous, stoppable)
        current = "Monitor"
        await emit("stage_enter", "Monitor", "Monitoring deployment")
        monitors.start(name)   # reset any stale stop flag from a prior run of this name
        prev_fail_keys: set = set()
        for _ in range(MONITOR_MAX_CYCLES):
            failures = await asyncio.to_thread(monitor.detect_failures, name, ns)
            metrics = await asyncio.to_thread(monitor.get_metrics, name, ns)
            await emit("health", "Monitor", "Health snapshot",
                       {"failures": failures, "metrics": metrics})
            for f in failures:
                if (f["pod"], f["type"]) not in prev_fail_keys:
                    await emit("failure", "Monitor", f"{f['type']} on {f['pod']}", f)
            prev_fail_keys = {(f["pod"], f["type"]) for f in failures}
            if monitors.is_stopped(name):
                break
            await asyncio.sleep(MONITOR_INTERVAL_S)
        await emit("stage_exit", "Monitor", "Monitoring stopped")
    except Exception as e:
        await emit("error", current, f"Unexpected error: {e}")
