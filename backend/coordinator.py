import asyncio
from events import Event, EventBus
from tools import manifests, validate, deploy

ROLLOUT_TIMEOUT_S = 120
POLL_INTERVAL_S = 2

async def run(cfg: dict, bus: EventBus) -> None:
    name, ns = cfg["name"], cfg.get("namespace", "default")
    port = int(cfg.get("port", 8080))

    async def emit(type_, stage, message, data=None):
        await bus.publish(Event(type=type_, stage=stage, message=message, data=data or {}))

    try:
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

        # Deploy
        current = "Deploy"
        await emit("stage_enter", "Deploy", "Applying to cluster")
        await emit("command", "Deploy", f"helm upgrade --install {name} chart")
        await asyncio.to_thread(deploy.install, cfg)
        await emit("stage_exit", "Deploy", "Applied to cluster")

        # Verify (rollout watch with timeout)
        current = "Verify"
        await emit("stage_enter", "Verify", "Waiting for rollout")
        last = None
        for _ in range(ROLLOUT_TIMEOUT_S // POLL_INTERVAL_S):
            ready, desired = await asyncio.to_thread(deploy.get_replicas, name, ns)
            if (ready, desired) != last:
                await emit("rollout", "Verify", f"{ready}/{desired} ready",
                           {"ready": ready, "desired": desired})
                last = (ready, desired)
            if desired and ready >= desired:
                break
            await asyncio.sleep(POLL_INTERVAL_S)
        else:
            await emit("error", "Verify", "Rollout did not complete in time",
                       {"timeout_s": ROLLOUT_TIMEOUT_S})
            return

        ep = await asyncio.to_thread(deploy.get_endpoint, name, ns, port)
        await emit("endpoint", "Verify", "Deployment is live", ep)
        await emit("stage_exit", "Verify", "Done")
    except Exception as e:  # surface, never hang
        await emit("error", current, f"Unexpected error: {e}")
