import asyncio
import os
import traceback
from events import Event, EventBus
from tools import manifests, validate, deploy, monitor, rollback, scan, cost, portforward, builder, gateway
import remediation
import diagnostics
import kubeconfig_store
import store
from breakers import Breaker
from approvals import Approvals
from monitors import Monitors
from agents import error_resolver, stack_reviewer
import guardrails

ROLLOUT_TIMEOUT_S = 120
POLL_INTERVAL_S = 2
MONITOR_INTERVAL_S = 5
MONITOR_MAX_CYCLES = 720   # safety cap (~1h at 5s); real stop is the Monitors flag

async def run(cfg: dict, bus: EventBus, approvals: Approvals, monitors: Monitors, breakers: Breaker) -> None:
    if cfg.get("services"):
        return await _run_compose(cfg, bus, approvals, monitors, breakers)
    name, ns = cfg["name"], cfg.get("namespace", "default")
    port = int(cfg.get("port", 8080))
    mode = cfg.get("mode", "manual")
    variants = guardrails.secret_variants(cfg.get("secrets") or {})
    current = "Detect"

    _ctx = {"cmd": ""}   # last command emitted — attached to errors for the extract report

    async def emit(type_, stage, message, data=None):
        if type_ == "command":
            _ctx["cmd"] = message
        ev = Event(type=type_, stage=stage,
                   message=guardrails.redact(message, variants),
                   data=guardrails.redact(data or {}, variants))
        await bus.publish(ev)
        await store.append_event(ev.to_dict())   # durable history (best-effort, redacted)

    explained: set = set()

    async def _emit_guidance(stage, issues, failure=None):
        # Self-healing "guide" rung: a break we can't auto-fix still gets clear,
        # actionable guidance so the user knows exactly what to change. Deterministic
        # catalog ALWAYS produces guidance; the LLM error-resolver only enriches it,
        # best-effort. A missing ANTHROPIC_API_KEY (or any LLM error) is swallowed —
        # we never leak an SDK auth error into the feed.
        # For a runtime crash, pull the CRASHED container's real logs first so the diagnosis
        # names the actual cause (e.g. postgres needs a password) and the fix-prompt carries
        # the verbatim error — not a generic "check the logs".
        logs = ""
        if failure:
            try:
                logs = await asyncio.to_thread(monitor.crash_logs, name, ns)
            except Exception:
                logs = ""
        g = diagnostics.diagnose(stage, issues,
                                 {"name": name, "image": cfg.get("image", ""), "namespace": ns}, logs=logs)
        try:
            ctx = {"failure_type": (failure.get("type") if failure else f"{stage}Failed") or "",
                   "pod_status": (failure.get("pod") if failure else ""),
                   "recent_events": (failure.get("message") if failure
                                     else "; ".join(str(i) for i in (issues if isinstance(issues, list) else [issues]))),
                   "recent_logs": logs,
                   "config_summary": f"{name} image={cfg.get('image','')} replicas={cfg.get('replicas','')}"}
            ai = await asyncio.to_thread(error_resolver.resolve, ctx)
            g["ai"] = {"root_cause": ai.get("root_cause", ""),
                       "recommended_action": ai.get("recommended_action", "")}
        except Exception:
            pass  # LLM unavailable (e.g. no ANTHROPIC_API_KEY) — deterministic guidance stands
        await emit("guidance", stage, g["summary"], g)

    async def explain(failure):
        # runtime pod failure (CrashLoopBackOff / ImagePull / OOM / …) -> actionable guidance
        key = (failure.get("pod"), failure.get("type"))
        if key in explained:
            return
        explained.add(key)
        issue = f"{failure.get('type','failure')} on {failure.get('pod','')}: {failure.get('message','')}".strip()
        await _emit_guidance(current, [issue], failure)

    async def guide(stage, issues):
        await _emit_guidance(stage, issues)

    async def remediate(reason):
        rstage = "Remediate"
        await emit("stage_enter", rstage, "Attempting auto-recovery")
        if breakers.tripped(name):
            await emit("escalation", rstage,
                       "Circuit breaker tripped — auto-remediation frozen, human needed")
            await emit("stage_exit", rstage, "Frozen")
            return
        revs = await asyncio.to_thread(rollback.get_revisions, name, ns)
        prior = rollback.previous_good_revision(revs)
        if prior is None:
            await emit("escalation", rstage,
                       "No prior good revision to roll back to — human needed")
            await emit("stage_exit", rstage, "Escalated")
            return
        action = "rollback"
        if remediation.is_destructive(action):   # rollback is safe; guards future actions
            await emit("escalation", rstage,
                       f"Action '{action}' is destructive — human-gated, not auto-run")
            await emit("stage_exit", rstage, "Gated")
            return
        breakers.record(name)
        await emit("remediation", rstage,
                   f"Rolling back {name} to revision {prior} (cause: {reason})",
                   {"revision": prior})
        try:
            await asyncio.to_thread(rollback.do_rollback, name, ns, prior)
            await emit("remediation", rstage,
                       f"Rolled back to revision {prior} — recovered", {"revision": prior})
        except Exception as e:
            await emit("escalation", rstage, f"Rollback failed: {e} — human needed")
        await emit("stage_exit", rstage, "Done")

    # a new deploy halts any prior deploy's monitor loop + port-forward (single-deploy
    # design) so stale health/URLs from an earlier app don't bleed into this one.
    monitors.stop_all()
    await asyncio.to_thread(portforward.stop_all)

    kubeconfig_tmp = None
    prev_kubeconfig = os.environ.get("KUBECONFIG")
    try:
        cluster = cfg.get("cluster") or ""
        if cluster:
            kubeconfig_tmp = await asyncio.to_thread(kubeconfig_store.decrypt_to_tempfile, cluster)
            os.environ["KUBECONFIG"] = kubeconfig_tmp   # ponytail: global; single-deploy by design (§status). Per-deploy env if concurrency added.

        # Detect capabilities and disable what the cluster can't serve
        current = "Detect"
        await emit("stage_enter", "Detect", "Checking cluster capabilities")
        await emit("command", "Detect", "kubectl version -o json --request-timeout=5s")
        # Preflight: fail fast + visibly if the cluster API is unreachable, instead of
        # stalling on a downstream kubectl/helm call with no feedback.
        reachable, detail = await asyncio.to_thread(deploy.cluster_reachable)
        if not reachable:
            target = ("cluster '" + cluster + "'") if cluster else "the local cluster"
            await emit("error", "Detect",
                       f"Can't reach {target}: {detail}. Check your kubeconfig/context and that the cluster is running.")
            await guide("Detect", [f"connection to {target} failed: {detail}"])
            return
        await emit("info", "Detect", f"Cluster reachable ({detail})")
        await emit("command", "Detect", "kubectl get ingressclass; kubectl get apiservices v1beta1.metrics.k8s.io")
        caps = await asyncio.to_thread(deploy.detect_capabilities)
        if cfg.get("ingress_host") and not caps["ingress_controller"]:
            await emit("info", "Detect",
                       "No ingress controller — skipping Ingress, use port-forward")
            cfg["ingress_host"] = ""
        if cfg.get("hpa_enabled") and not caps["metrics_server"]:
            await emit("info", "Detect", "No metrics-server — skipping HPA")
            cfg["hpa_enabled"] = False
        await emit("stage_exit", "Detect", "Capabilities resolved")

        # Build (deploy-from-source) — clone the repo, build its Dockerfile, and make the
        # image available to the cluster. Skipped entirely when a pre-built image is given.
        if cfg.get("git_repo"):
            current = "Build"
            workdir = None
            await emit("stage_enter", "Build", "Building image from source")
            try:
                repo, branch = cfg["git_repo"], cfg.get("git_branch", "")
                ref, dockerfile = cfg.get("git_ref", ""), cfg.get("dockerfile") or ""
                safe = builder.display_url(repo)
                await emit("command", "Build",
                           f"git clone --depth 1 {('-b ' + branch + ' ') if branch else ''}{safe}")
                workdir, sha = await asyncio.to_thread(builder.clone, repo, branch, ref)
                # build context is ALWAYS the repo root (Docker norm — a Dockerfile in a
                # subdir still COPYs from the root). A tree-URL subfolder is only a hint for
                # WHICH Dockerfile to auto-pick; the path stays root-relative.
                subdir = cfg.get("git_subdir") or ""
                if subdir and (".." in subdir or not os.path.isdir(os.path.join(workdir, subdir))):
                    subdir = ""   # bogus/missing subdir -> ignore, detect across the whole repo
                if not dockerfile:
                    found = await asyncio.to_thread(builder.list_dockerfiles, workdir)  # root-relative
                    pool = [f for f in found if subdir and f.startswith(subdir + "/")] or found
                    if "Dockerfile" in pool:
                        dockerfile = "Dockerfile"
                    elif len(pool) == 1:
                        dockerfile = pool[0]
                        await emit("info", "Build", f"Auto-detected Dockerfile: {dockerfile}")
                    elif not pool:
                        await emit("error", "Build", "No Dockerfile found in the repository")
                        await guide("Build", ["Dockerfile not found anywhere in the repository"])
                        return
                    else:
                        listing = ", ".join(pool)
                        await emit("error", "Build",
                                   f"Multiple Dockerfiles found — pick one and re-deploy: {listing}",
                                   {"dockerfiles": pool})
                        await guide("Build", [f"multiple Dockerfiles found: {listing}"])
                        return
                tag = builder.image_tag(name, sha)
                await emit("command", "Build", f"docker build -t {tag} -f {dockerfile} .")
                await asyncio.to_thread(builder.build, workdir, tag, dockerfile)
                ctx = await asyncio.to_thread(builder.current_context)
                await emit("command", "Build", f"# load {tag} into cluster (context {ctx})")
                method = await asyncio.to_thread(builder.make_available, tag, ctx)
                cfg["image"] = tag
                await emit("info", "Build", f"Built {tag} from {safe}@{sha} → available via {method}")
                await emit("stage_exit", "Build", "Image built")
            except Exception as e:
                await emit("error", "Build", f"Build failed: {e}")
                await guide("Build", [f"source build failed: {e}"])
                return
            finally:
                if workdir:
                    await asyncio.to_thread(builder.cleanup, workdir)

        # Generate
        current = "Generate"
        await emit("stage_enter", "Generate", "Rendering manifests via Helm")
        await emit("command", "Generate", f"helm template {name} ./chart -f values.yaml -n {ns}")
        rendered = await asyncio.to_thread(manifests.render, cfg)
        await emit("manifest", "Generate", "Rendered manifests", {"yaml": rendered})
        estimate = await asyncio.to_thread(cost.estimate, rendered)
        await emit("cost", "Generate",
                   f"Estimated ${estimate['monthly_usd']}/mo", estimate)
        await emit("stage_exit", "Generate", "Manifests ready")

        # Validate
        current = "Validate"
        await emit("stage_enter", "Validate", "Validating manifests")
        await emit("command", "Validate", "kubeconform -strict -  |  kubectl apply --dry-run=server -f -  |  kube-score score -")
        ok, issues = await asyncio.to_thread(validate.validate, rendered, ns)
        if not ok:
            await emit("error", "Validate", "Validation failed", {"issues": issues})
            await guide("Validate", issues)
            return
        await emit("stage_exit", "Validate", "Validation passed")

        # Approve
        current = "Approve"
        await emit("stage_enter", "Approve", "Approval stage")
        if mode == "manual":
            # register the Future BEFORE announcing — else a fast POST /approve during
            # emit's await points finds no pending entry, drops the approval, and the
            # deploy hangs forever on a Future nobody will resolve.
            fut = approvals.create(name)
            await emit("approval_required", "Approve",
                       f"Approve deployment of {name} to {ns}?",
                       {"name": name, "namespace": ns})
            approved = await fut
            if not approved:
                await emit("rejected", "Approve", "Deployment rejected by user")
                return
            await emit("stage_exit", "Approve", "Approved")
        else:
            await emit("info", "Approve", "Autonomous mode — auto-approved")
            await emit("stage_exit", "Approve", "Approved")

        # Scan (image vulns gate + advisory misconfig)
        current = "Scan"
        await emit("stage_enter", "Scan", "Scanning image and manifests")
        await emit("command", "Scan", f"trivy image --severity HIGH,CRITICAL {cfg['image']}")
        img_scan = await asyncio.to_thread(scan.scan_image, cfg["image"])
        cfg_scan = await asyncio.to_thread(scan.scan_config, rendered)
        await emit("scan", "Scan", img_scan["summary"],
                   {"image": img_scan, "config": cfg_scan})
        if img_scan["available"] and not img_scan["ok"]:
            if cfg.get("allow_vulnerable"):
                await emit("info", "Scan",
                           f"Image has findings ({img_scan['summary']}) — proceeding: operator "
                           f"set allow_vulnerable. Findings still reported above.")
            else:
                await emit("error", "Scan",
                           f"Image scan gate failed: {img_scan['summary']}",
                           {"findings": img_scan["findings"]})
                await guide("Scan", [img_scan["summary"]] +
                            [f"{f.get('severity','')} {f.get('id','')} {f.get('pkg','')}"
                             for f in img_scan["findings"][:5]])
                return
        if not img_scan["available"]:
            await emit("info", "Scan", "trivy not installed — image scan skipped (not a pass)")
        await emit("stage_exit", "Scan", "Scan complete")

        # Deploy
        current = "Deploy"
        await emit("stage_enter", "Deploy", "Applying to cluster")
        await emit("command", "Deploy", f"helm upgrade --install {name} ./chart -n {ns} --create-namespace")
        await asyncio.to_thread(deploy.install, cfg)
        await emit("stage_exit", "Deploy", "Applied to cluster")

        # Verify
        current = "Verify"
        await emit("stage_enter", "Verify", "Waiting for rollout")
        await emit("command", "Verify", f"kubectl get deploy {name} -n {ns} -o json   # poll readyReplicas")
        last = None
        seen_failures: set = set()
        for _ in range(ROLLOUT_TIMEOUT_S // max(POLL_INTERVAL_S, 1)):
            ready, desired = await asyncio.to_thread(deploy.get_replicas, name, ns)
            if (ready, desired) != last:
                await emit("rollout", "Verify", f"{ready}/{desired} ready",
                           {"ready": ready, "desired": desired})
                last = (ready, desired)
            for f in await asyncio.to_thread(monitor.detect_failures, name, ns):
                key = (f["pod"], f["type"])
                if key not in seen_failures:
                    seen_failures.add(key)
                    # WARN only — a pod may crash-loop transiently during startup and
                    # still recover. Don't emit blocking "fix this" guidance here; that
                    # comes only if the rollout ultimately fails (the timeout branch).
                    await emit("failure", "Verify", f"{f['type']} on {f['pod']}", f)
            if desired and ready >= desired:
                break
            await asyncio.sleep(POLL_INTERVAL_S)
        else:
            failures = await asyncio.to_thread(monitor.detect_failures, name, ns)
            await emit("error", "Verify", "Rollout did not complete in time",
                       {"timeout_s": ROLLOUT_TIMEOUT_S, "failures": failures})
            # persistent failure -> actionable guidance (both modes)
            await guide("Verify",
                        [f"{f['type']} on {f['pod']}: {f.get('message','')}" for f in failures]
                        or ["rollout did not reach the desired replica count in time"])
            if mode == "autonomous":
                await remediate("rollout did not complete")
            return

        # all desired replicas ready -> genuinely live
        ep = await asyncio.to_thread(deploy.get_endpoint, name, ns, port)
        try:
            lport = await asyncio.to_thread(portforward.start, name, ns, f"svc/{name}", port)
            ep["url"] = f"http://127.0.0.1:{lport}"
            # V4: actually hit it — "ready" (probe passed) plus "answers a real request" is proof.
            status = await asyncio.to_thread(deploy.probe_url, ep["url"])
            ep["responding"] = status is not None
            await emit("info", "Verify",
                       (f"Confirmed the app responds (HTTP {status})." if status
                        else "Rollout is healthy, but the app didn't answer an HTTP request yet — it may still be warming up or not speak HTTP."))
        except Exception:
            pass  # port-forward is best-effort; the service/port-forward cmd still shown
        if seen_failures:
            kinds = ", ".join(sorted({k[1] for k in seen_failures}))
            await emit("info", "Verify",
                       f"Pods hit {kinds} during startup but the rollout recovered — all {desired} replicas are ready.")
        await emit("endpoint", "Verify", "Deployment is live", ep)
        await emit("stage_exit", "Verify", "Done")
        # a healthy rollout clears the auto-remediation breaker so it counts CONSECUTIVE
        # failed remediations, not lifetime ones (else self-healing freezes forever).
        breakers.reset(name)

        # Monitor (continuous, stoppable)
        current = "Monitor"
        await emit("stage_enter", "Monitor", "Monitoring deployment")
        await emit("command", "Monitor", f"kubectl get pods -l app.kubernetes.io/name={name} -n {ns}; kubectl top pods")
        monitors.start(name)   # reset any stale stop flag from a prior run of this name
        prev_fail_keys: set = set()
        for _ in range(MONITOR_MAX_CYCLES):
            # check stop FIRST — a new deploy calls monitors.stop_all() while this loop is
            # parked in sleep; on wake it must exit before querying/emitting, else a stale
            # snapshot of the OLD app leaks into the new deploy's live stream.
            if monitors.is_stopped(name):
                break
            failures = await asyncio.to_thread(monitor.detect_failures, name, ns)
            metrics = await asyncio.to_thread(monitor.get_metrics, name, ns)
            await emit("health", "Monitor", "Health snapshot",
                       {"failures": failures, "metrics": metrics})
            for f in failures:
                if (f["pod"], f["type"]) not in prev_fail_keys:
                    await emit("failure", "Monitor", f"{f['type']} on {f['pod']}", f)
                    await explain(f)
            prev_fail_keys = {(f["pod"], f["type"]) for f in failures}
            await asyncio.sleep(MONITOR_INTERVAL_S)
        await emit("stage_exit", "Monitor", "Monitoring stopped")
    except Exception as e:
        try:
            # capture the RAW detail so the user can hand the AI building this project
            # an exact root cause: the traceback (which platform file:line broke),
            # the failing subprocess's stderr, and the command that was running.
            tb = traceback.format_exc()
            stderr = getattr(e, "stderr", "") or ""
            await emit("error", current, f"Unexpected error: {e}",
                       {"kind": "internal", "command": _ctx["cmd"],
                        "stderr": stderr, "traceback": tb})
            await guide(current, [f"internal error: {e}"])
        except Exception:
            pass
    finally:
        if kubeconfig_tmp:
            if prev_kubeconfig is not None:
                os.environ["KUBECONFIG"] = prev_kubeconfig
            else:
                os.environ.pop("KUBECONFIG", None)
            try:
                os.unlink(kubeconfig_tmp)
            except OSError:
                pass


async def _run_compose(cfg: dict, bus: EventBus, approvals: Approvals,
                       monitors: Monitors, breakers: Breaker) -> None:
    """Deploy a docker-compose stack: N services, one namespace, one Helm release each.
    Shared Detect + Approve + Monitor; per-service Generate/Validate/Scan/Deploy/Verify.
    Service-to-service DNS is free (each K8s Service is named after its compose service)."""
    stack, ns = cfg["name"], cfg.get("namespace", "default")
    mode = cfg.get("mode", "manual")
    services = cfg["services"]
    warnings = cfg.get("warnings") or []
    # redact EVERY service's secrets everywhere (union) — a secret from one service must
    # never leak in another's stream either.
    all_secrets = {}
    for s in services:
        all_secrets.update(s.get("secrets") or {})
    variants = guardrails.secret_variants(all_secrets)
    _ctx = {"cmd": ""}

    async def emit(type_, stage, message, data=None):
        if type_ == "command":
            _ctx["cmd"] = message
        ev = Event(type=type_, stage=stage,
                   message=guardrails.redact(message, variants),
                   data=guardrails.redact(data or {}, variants))
        await bus.publish(ev)
        await store.append_event(ev.to_dict())

    async def guide(stage, issues):
        g = diagnostics.diagnose(stage, issues, {"name": stack, "image": "", "namespace": ns})
        await emit("guidance", stage, g["summary"], g)

    svc_by_name = {s["name"]: s for s in services}

    async def guide_crash(stage, sn):
        # a crashing service -> fetch its CRASHED container's real logs and diagnose the actual
        # cause (postgres-needs-password, connection-refused, …) with the logs in the fix-prompt.
        svc = svc_by_name.get(sn) or {}
        try:
            logs = await asyncio.to_thread(monitor.crash_logs, sn, ns)
        except Exception:
            logs = ""
        g = diagnostics.diagnose(stage, [f"CrashLoopBackOff on {sn}"],
                                 {"name": sn, "image": svc.get("image", ""), "namespace": ns}, logs=logs)
        await emit("guidance", stage, g["summary"], g)

    monitors.stop_all()
    await asyncio.to_thread(portforward.stop_all)

    kubeconfig_tmp = None
    prev_kubeconfig = os.environ.get("KUBECONFIG")
    try:
        cluster = cfg.get("cluster") or ""
        if cluster:
            kubeconfig_tmp = await asyncio.to_thread(kubeconfig_store.decrypt_to_tempfile, cluster)
            os.environ["KUBECONFIG"] = kubeconfig_tmp

        # Detect (once)
        await emit("stage_enter", "Detect", f"Deploying compose stack '{stack}' ({len(services)} services)")
        for svc in services:
            await emit("info", "Detect", f"• {svc['name']} → {svc['image']}"
                       + (f" (:{svc['port']})" if svc.get("published") else ""))
        for w in warnings:
            await emit("info", "Detect", f"⚠ {w}")
        reachable, detail = await asyncio.to_thread(deploy.cluster_reachable)
        if not reachable:
            target = ("cluster '" + cluster + "'") if cluster else "the local cluster"
            await emit("error", "Detect", f"Can't reach {target}: {detail}.")
            await guide("Detect", [f"connection to {target} failed: {detail}"])
            return
        await emit("info", "Detect", f"Cluster reachable ({detail})")
        # per-service capability downgrade: an Ingress with no controller / an HPA with no
        # metrics-server would silently not work — detect, warn, and skip (edge cases §13).
        caps = await asyncio.to_thread(deploy.detect_capabilities)
        for svc in services:
            if svc.get("ingress_host") and not caps["ingress_controller"]:
                await emit("info", "Detect", f"{svc['name']}: no ingress controller — skipping Ingress, use port-forward")
                svc["ingress_host"] = ""
            if svc.get("hpa_enabled") and not caps["metrics_server"]:
                await emit("info", "Detect", f"{svc['name']}: no metrics-server — skipping HPA")
                svc["hpa_enabled"] = False
        await emit("stage_exit", "Detect", "Ready")

        # Build (per-service, from source) — services carrying a build spec instead of a pre-built
        # image. Clone each distinct repo ONCE (a monorepo shared by several services), build each
        # service's Dockerfile with its own build context, load the image into the cluster, and
        # fill svc["image"] so Generate/Approve see the real tag. Same builder as the single path.
        build_svcs = [s for s in services if s.get("build")]
        if build_svcs:
            stack_repo = cfg.get("git_repo", "")
            stack_subdir = cfg.get("git_subdir", "") or ""
            await emit("stage_enter", "Build", f"Building {len(build_svcs)} service(s) from source")
            kctx = await asyncio.to_thread(builder.current_context)
            clones: dict = {}   # (repo,branch,ref) -> (workdir, sha)
            try:
                for svc in build_svcs:
                    b = svc["build"]
                    # defense-in-depth against argv flag-smuggling: reject any value that would
                    # be read as a flag by git/docker. builder.clone/build also validate at the
                    # choke point (URL regex, no-leading-dash refs, `--` separator, absolute -f
                    # path), but this fails fast + clearly at the untrusted-input boundary.
                    for _fld in ("git_repo", "git_branch", "git_ref", "dockerfile", "subdir"):
                        if str(b.get(_fld) or "").startswith("-"):
                            await emit("error", "Build", f"{svc['name']}: build.{_fld} must not start with '-'")
                            await guide("Build", [f"{svc['name']}: invalid build.{_fld} (leading '-')"])
                            return
                    own_repo = b.get("git_repo") or ""
                    repo = own_repo or stack_repo
                    if not repo:
                        await emit("error", "Build",
                                   f"{svc['name']}: build service has no git_repo and the stack wasn't deployed from a repo")
                        await guide("Build", [f"{svc['name']}: source build has no git repository to build from"])
                        return
                    branch, ref = b.get("git_branch", ""), b.get("git_ref", "")
                    key = (repo, branch, ref)
                    if key not in clones:
                        safe = builder.display_url(builder.normalize_repo_url(repo)[0])
                        await emit("command", "Build",
                                   f"git clone --depth 1 {('-b ' + branch + ' ') if branch else ''}{safe}")
                        clones[key] = await asyncio.to_thread(builder.clone, repo, branch, ref)
                    workdir, sha = clones[key]
                    # build context = repo/[stack_subdir/]svc_subdir. stack_subdir only applies when
                    # the service inherits the stack repo (a compose file that lived in a subfolder).
                    sub = b.get("subdir", "") or ""
                    parts = [p for p in ((stack_subdir if not own_repo else ""), sub) if p]
                    ctxdir = os.path.join(workdir, *parts) if parts else workdir
                    # path-traversal guard: the resolved build context MUST stay inside the clone.
                    # `sub` and `stack_subdir` (the latter from a pasted tree URL) could contain
                    # '..' and escape the workdir, exposing arbitrary host dirs as the build context.
                    root = os.path.realpath(workdir)
                    real_ctx = os.path.realpath(ctxdir)
                    if real_ctx != root and not real_ctx.startswith(root + os.sep):
                        await emit("error", "Build", f"{svc['name']}: build context escapes the repo (bad subdir '{sub}')")
                        await guide("Build", [f"{svc['name']}: build context '{sub}' escapes the repository"])
                        return
                    if not os.path.isdir(real_ctx):
                        await emit("error", "Build", f"{svc['name']}: build context '{sub}' not found in the repo")
                        await guide("Build", [f"{svc['name']}: build context directory '{sub}' not found"])
                        return
                    dockerfile = b.get("dockerfile", "") or ""
                    if not dockerfile:
                        found = await asyncio.to_thread(builder.list_dockerfiles, ctxdir)
                        if "Dockerfile" in found:
                            dockerfile = "Dockerfile"
                        elif len(found) == 1:
                            dockerfile = found[0]
                            await emit("info", "Build", f"{svc['name']}: auto-detected {dockerfile}")
                        elif not found:
                            await emit("error", "Build", f"{svc['name']}: no Dockerfile found in the build context")
                            await guide("Build", [f"{svc['name']}: Dockerfile not found in the build context"])
                            return
                        else:
                            listing = ", ".join(found)
                            await emit("error", "Build",
                                       f"{svc['name']}: multiple Dockerfiles — set build.dockerfile: {listing}",
                                       {"dockerfiles": found})
                            await guide("Build", [f"{svc['name']}: multiple Dockerfiles found: {listing}"])
                            return
                    tag = builder.image_tag(f"{stack}-{svc['name']}", sha)
                    bargs = svc.get("build_args") or {}   # auto-wired browser base -> baked correctly
                    argstr = " ".join(f"--build-arg {k}={v}" for k, v in bargs.items())
                    await emit("command", "Build", f"docker build -t {tag} -f {dockerfile} {argstr} .".replace("  ", " "))
                    await asyncio.to_thread(builder.build, ctxdir, tag, dockerfile, bargs)
                    method = await asyncio.to_thread(builder.make_available, tag, kctx)
                    svc["image"] = tag
                    await emit("info", "Build", f"{svc['name']}: built {tag} → available via {method}")
                await emit("stage_exit", "Build", "Images built")
            except Exception as e:
                await emit("error", "Build", f"Build failed: {e}")
                await guide("Build", [f"source build failed: {e}"])
                return
            finally:
                for wd, _sha in clones.values():
                    await asyncio.to_thread(builder.cleanup, wd)

        # AI-brain advisory (Phase 3): a stack reviewer flags multi-service wiring problems the
        # deterministic rules can't see (an app depending on a service that isn't in the stack,
        # etc.). Best-effort — swallowed if there's no ANTHROPIC_API_KEY; ADVISORY ONLY, never
        # blocks (the locked invariant: the LLM proposes, deterministic code disposes). Fed only
        # the structure (names/images/env KEY names) — never secret values.
        if len(services) > 1:
            try:
                rev = await asyncio.to_thread(stack_reviewer.review, services)
                for f in (rev.get("findings") or [])[:10]:
                    await emit("info", "Detect",
                               f"💡 reviewer — {f.get('service','')}: {f.get('issue','')} → {f.get('suggestion','')}")
            except Exception:
                pass   # LLM unavailable / no API key -> the deterministic rules stand on their own

        # Proactive guard: a known database image with no password set WILL crash-loop on boot
        # (the #1 cause of a DB CrashLoopBackOff). Warn loudly before we deploy so it's caught up
        # front rather than after the crash.
        for svc in services:
            hint = diagnostics.db_required_env_missing(
                svc.get("image", ""), {**(svc.get("env") or {}), **(svc.get("secrets") or {})})
            if hint:
                await emit("info", "Detect",
                           f"⚠ {svc['name']}: this database needs {hint} — set it or the pod will crash on start.")

        # Approve (once, whole stack)
        if mode == "manual":
            fut = approvals.create(stack)
            await emit("approval_required", "Approve",
                       f"Approve deploying {len(services)} services to {ns}?",
                       {"name": stack, "namespace": ns,
                        "services": [s["name"] for s in services]})
            if not await fut:
                await emit("rejected", "Approve", "Stack deployment rejected by user")
                return
            await emit("stage_exit", "Approve", "Approved")
        else:
            await emit("info", "Approve", "Autonomous mode — auto-approved")

        # Per service: Generate → Validate → Scan → Deploy (in depends_on order)
        for svc in services:
            sn = svc["name"]
            scfg = {**svc, "namespace": ns, "cluster": cluster, "stack": stack}
            st = lambda s: f"{sn}:{s}"
            await emit("stage_enter", st("Generate"), f"Rendering {sn}")
            rendered = await asyncio.to_thread(manifests.render, scfg)
            await emit("manifest", st("Generate"), f"Rendered {sn}", {"yaml": rendered})
            await emit("stage_exit", st("Generate"), "Manifests ready")

            await emit("stage_enter", st("Validate"), f"Validating {sn}")
            ok, issues = await asyncio.to_thread(validate.validate, rendered, ns)
            if not ok:
                await emit("error", st("Validate"), f"Validation failed for {sn}", {"issues": issues})
                await guide(st("Validate"), issues)
                return
            await emit("stage_exit", st("Validate"), "Valid")

            await emit("stage_enter", st("Scan"), f"Scanning {sn}")
            img_scan = await asyncio.to_thread(scan.scan_image, svc["image"])
            await emit("scan", st("Scan"), img_scan["summary"], {"image": img_scan})
            if img_scan["available"] and not img_scan["ok"]:
                if cfg.get("allow_vulnerable"):
                    await emit("info", st("Scan"),
                               f"{sn} image has findings ({img_scan['summary']}) — proceeding "
                               f"(operator set allow_vulnerable).")
                else:
                    await emit("error", st("Scan"), f"Image scan gate failed for {sn}: {img_scan['summary']}",
                               {"findings": img_scan["findings"]})
                    await guide(st("Scan"), [img_scan["summary"]])
                    return
            await emit("stage_exit", st("Scan"), "Scanned")

            await emit("stage_enter", st("Deploy"), f"Applying {sn}")
            await emit("command", st("Deploy"),
                       f"helm upgrade --install {sn} ./chart -n {ns} --create-namespace")
            try:
                await asyncio.to_thread(deploy.install, scfg)
            except Exception as e:
                await emit("error", st("Deploy"), f"Deploy of {sn} failed: {e}")
                await guide(st("Deploy"), [f"helm install failed for {sn}: {e}"])
                return
            await emit("stage_exit", st("Deploy"), "Applied")

        # Verify all rollouts (shared timeout budget). A cronjob has no Deployment to roll out —
        # it's "scheduled", not "ready", so it's excluded from the wait (else it hangs forever).
        await emit("stage_enter", "Verify", "Waiting for all services to become ready")
        for svc in services:
            if svc.get("workload") == "cronjob":
                await emit("info", "Verify", f"{svc['name']}: CronJob scheduled ({svc.get('schedule','')})")
        pending = {s["name"] for s in services if s.get("workload") != "cronjob"}
        last = {}
        for _ in range(ROLLOUT_TIMEOUT_S // max(POLL_INTERVAL_S, 1)):
            for sn in list(pending):
                ready, desired = await asyncio.to_thread(deploy.get_replicas, sn, ns)
                if (ready, desired) != last.get(sn):
                    await emit("rollout", "Verify", f"{sn}: {ready}/{desired} ready",
                               {"service": sn, "ready": ready, "desired": desired})
                    last[sn] = (ready, desired)
                if desired and ready >= desired:
                    pending.discard(sn)
            if not pending:
                break
            await asyncio.sleep(POLL_INTERVAL_S)
        if pending:
            failures = []
            for sn in pending:
                failures += await asyncio.to_thread(monitor.detect_failures, sn, ns)
            await emit("error", "Verify", f"Services not ready in time: {', '.join(sorted(pending))}",
                       {"pending": sorted(pending), "failures": failures})
            # per-service crash diagnosis from the REAL logs (names the actual cause + fix-prompt)
            for sn in sorted(pending):
                await guide_crash("Verify", sn)
            return

        # Port-forward each browser-facing service; emit an endpoint per forward. A frontend that
        # makes BROWSER calls to a backend (ingress_routes) gets a same-origin reverse-proxy
        # GATEWAY (works with no ingress controller) so /api reaches the backend — then we forward
        # the gateway, not the frontend. This is the universal browser↔backend fix.
        endpoints = {}
        for svc in services:
            if not svc.get("published"):
                continue
            sn, sport = svc["name"], int(svc["port"])
            routes = svc.get("ingress_routes")
            fwd_name, fwd_port, via = sn, sport, "svc/" + sn
            if routes:
                try:
                    gw = await asyncio.to_thread(gateway.deploy_gateway, stack, ns, sn, sport, routes)
                    fwd_name, fwd_port, via = gw, 80, "svc/" + gw
                    await emit("info", "Verify",
                               f"Wired {sn} through a same-origin gateway ("
                               + ", ".join(f"{r['path']}→{r['service']}" for r in routes) + ").")
                except Exception as e:
                    await emit("info", "Verify", f"Gateway setup failed ({e}); serving {sn} directly.")
            ep = await asyncio.to_thread(deploy.get_endpoint, fwd_name, ns, fwd_port)
            try:
                lport = await asyncio.to_thread(portforward.start, fwd_name, ns, via, fwd_port)
                ep["url"] = f"http://127.0.0.1:{lport}"
                endpoints[sn] = ep["url"]
                status = await asyncio.to_thread(deploy.probe_url, ep["url"])   # V4
                ep["responding"] = status is not None
                if status:
                    await emit("info", "Verify", f"Confirmed {sn} responds (HTTP {status}).")
            except Exception:
                pass
            ep["service_name"] = sn
            await emit("endpoint", "Verify", f"{sn} is live", ep)

        # CHANGE 4 — SUCCESS = the user's paths work, not just "pods Running". Run the app-author's
        # smoke tests through the published entrypoints; any failure => the deploy FAILED (even
        # though every pod is Ready), with a structured, AI-pasteable error report.
        smoke = cfg.get("smoke_tests") or []
        if smoke:
            await emit("info", "Verify", f"Running {len(smoke)} smoke test(s) — the real check that the app works…")
            results = await asyncio.to_thread(deploy.run_smoke_tests, smoke, endpoints)
            failed = [r for r in results if not r["ok"]]
            for r in results:
                t = r["test"]
                if r["ok"]:
                    await emit("info", "Verify", f"✓ smoke: {t.get('path','/')} → {r['got']} ({t.get('proves','')})")
                else:
                    await emit("error", "Verify",
                               f"✗ smoke FAILED: {t.get('path','/')} expected {r['expect']} got {r.get('got')} "
                               f"— {t.get('proves','')}", {"smoke": r})
            if failed:
                first = failed[0]
                svc0 = str(first["test"].get("via") or (services[0]["name"]))
                svc_obj = svc_by_name.get(svc0) or services[0]
                report = await asyncio.to_thread(deploy.smoke_error_report, "Verify", svc0, ns,
                                                 first, svc_obj.get("secrets") or {})
                await emit("error", "Verify",
                           "Deployment FAILED — pods are Running but the app doesn't work end-to-end. "
                           "Paste this report to the AI that built the app.", {"error_report": report})
                await guide_crash("Verify", svc0)
                return
            await emit("info", "Verify", "All smoke tests passed — the app works end-to-end. 🎉")
        await emit("stage_exit", "Verify", "All services ready")

        # Monitor (once, all services)
        await emit("stage_enter", "Monitor", "Monitoring the stack")
        monitors.start(stack)
        prev_fail: set = set()
        for _ in range(MONITOR_MAX_CYCLES):
            if monitors.is_stopped(stack):
                break
            snapshot = {}
            fails_now: set = set()
            for svc in services:
                sn = svc["name"]
                failures = await asyncio.to_thread(monitor.detect_failures, sn, ns)
                metrics = await asyncio.to_thread(monitor.get_metrics, sn, ns)
                snapshot[sn] = {"failures": failures, "metrics": metrics}
                for f in failures:
                    fails_now.add((sn, f["pod"], f["type"]))
            await emit("health", "Monitor", "Stack health snapshot", {"services": snapshot})
            new = fails_now - prev_fail
            for key in new:
                await emit("failure", "Monitor", f"{key[2]} on {key[1]} ({key[0]})",
                           {"service": key[0], "pod": key[1], "type": key[2]})
            # automatically diagnose each newly-crashing service from its real logs (once per
            # service per batch) so the user gets the actual cause + a fix-prompt, not just "it's failing".
            for sn in {k[0] for k in new if k[2] in ("CrashLoopBackOff", "Error", "CreateContainerConfigError")}:
                await guide_crash("Monitor", sn)
            prev_fail = fails_now
            await asyncio.sleep(MONITOR_INTERVAL_S)
        await emit("stage_exit", "Monitor", "Monitoring stopped")
    except Exception as e:
        try:
            tb = traceback.format_exc()
            await emit("error", "Deploy", f"Unexpected error: {e}",
                       {"kind": "internal", "command": _ctx["cmd"], "traceback": tb})
            await guide("Deploy", [f"internal error: {e}"])
        except Exception:
            pass
    finally:
        if kubeconfig_tmp:
            if prev_kubeconfig is not None:
                os.environ["KUBECONFIG"] = prev_kubeconfig
            else:
                os.environ.pop("KUBECONFIG", None)
            try:
                os.unlink(kubeconfig_tmp)
            except OSError:
                pass
