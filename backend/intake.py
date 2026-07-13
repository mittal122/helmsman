"""Single-shot structured intake — the "developer is just a bridge" loop (master-prompt
Phase 1, Case 2: the app is already containerized).

Flow:
  1. build_prompt()  -> a copy-paste prompt the developer hands to the AI that built their
     app. It asks that AI to return ALL deployment info as ONE JSON blob in a fixed schema.
  2. ingest(json)    -> deterministically parse that returned JSON into the SAME normalized
     service cfgs the coordinator already consumes (identical shape to tools/compose.parse),
     compute a Missing list (never assume a value), and a human summary.

Design (locked invariants kept):
- The prompt is DETERMINISTIC (a constant template), not an LLM call — the schema is fixed,
  so there is nothing to "generate". Schema is defined ONCE here, next to the parser that
  reads it, so the two can't drift.
- ingest is deterministic + pure (touches no cluster, runs no repo code) and treats the
  pasted JSON as untrusted DATA: values are validated field-by-field, never executed and
  never sent to an LLM to act on. A field trying to look like an instruction is just a bad
  value.
- Output service cfgs match tools/compose.py exactly, so coordinator._run_compose renders
  and deploys them with zero new deploy machinery.

v1 scope (mirrors compose v1): pre-built images only (no build:), inline env/secrets,
ports, resources, health->probe, named volumes->PVC, replicas, command/args, run_as_user.
"""
import json
import re

import diagnostics

_RFC1123 = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_SECRETISH = re.compile(r"(?i)(PASSWORD|PASSWD|SECRET|TOKEN|CREDENTIAL|APIKEY|KEY)")


def _k8s_name(s: str) -> str:
    """Coerce any string to a valid RFC1123 label (dev-AI may return 'My App')."""
    s = re.sub(r"[^a-z0-9-]", "-", (s or "").lower()).strip("-")
    return s or "app"


# The exact JSON the developer's AI must return. Kept compact but complete. Every field the
# coordinator can use is listed so the dev-AI fills it in one shot (never re-questioned).
_SCHEMA_EXAMPLE = """{
  "application": { "name": "my-app", "namespace": "default" },
  "services": [
    {
      "name": "web",                        // REQUIRED, lowercase (becomes the K8s name + DNS)
      "type": "deployment",                 // deployment (served) | worker (background) | cronjob (scheduled)
      "image": "myorg/web:1.4.2",           // a pre-built image, OR omit it and give "build" below to build from source
      "build": { "git_repo": "https://github.com/org/web.git", "git_branch": "main", "subdir": "", "dockerfile": "" },
      "port": 3000,                          // REQUIRED for a deployment (the listen port); omit for worker/cronjob
      "schedule": "*/5 * * * *",            // cronjob only: cron expression
      "stop_grace_seconds": 30,             // optional: graceful-shutdown period
      "extra_ports": [9090],                // other container ports (optional)
      "replicas": 2,
      "published": true,                     // true = browser-facing (gets a port-forward)
      "env": { "APP_ENV": "prod" },         // non-secret config
      "secrets": { "DB_PASSWORD": "..." },  // sensitive values (redacted everywhere)
      "command": [],                         // entrypoint override (optional)
      "args": [],                            // command/args override (optional)
      "resources": {
        "requests": { "cpu": "100m", "memory": "128Mi" },
        "limits":   { "cpu": "500m", "memory": "256Mi" }
      },
      "health": { "type": "http", "path": "/healthz" },  // type: http|tcp|exec|none; exec uses "command": [...]
      "volumes": [ { "name": "data", "mountPath": "/var/lib/app", "size": "1Gi" } ],
      "run_as_user": null,                   // numeric UID if the image needs a specific user
      "ingress": { "host": "app.example.com" },          // browser-facing HTTP host (omit if internal-only)
      "scaling": { "min": 2, "max": 5, "cpu": 70 },      // CPU-based autoscaling (omit for a fixed replica count)
      "service_account": { "create": true, "annotations": {}, "rules": [] },  // omit unless the app needs a dedicated SA / cloud identity / k8s API access
      "depends_on": ["db"]                   // start-order hint (optional)
    }
  ]
}"""


def build_prompt(context: dict | None = None) -> str:
    """The copy-paste prompt the developer relays to the AI that built their app. Deterministic
    — the same request for every app, so no LLM is involved on our side.

    context.containerize=True adds a preamble telling the AI to containerize the app first (write
    Dockerfiles) when it isn't already, then return the SAME structured JSON. This is the whole
    point of the developer-as-bridge: we ask the app's own AI ONE prompt and it detects the
    language/framework/dependencies itself — the human is never asked a technical question."""
    c = context or {}
    app = (c.get("app_description") or "").strip()
    containerize = bool(c.get("containerize"))
    L = []
    L.append("I want to deploy my application to Kubernetes using an automated deployment "
             "agent. The agent will NOT read my code — it only consumes the structured JSON "
             "you produce below. You built (or fully understand) this application, so gather "
             "EVERYTHING needed to deploy it and return it in ONE JSON object.")
    if app:
        L.append("")
        L.append(f"Application: {app}")
    if containerize:
        L.append("")
        L.append("## First — containerize the app if it isn't already")
        L.append("- Figure out for yourself whether the app is containerized. If it already has "
                 "working image(s)/Dockerfile(s), skip to the JSON and report them.")
        L.append("- If it is NOT containerized: detect the architecture and EVERY component that "
                 "must run, and write a production-grade, multi-stage Dockerfile for each — a "
                 "minimal pinned base image (never ':latest'), a non-root user, only production "
                 "dependencies, plus a .dockerignore. Commit these files to the repository.")
        L.append("- For a component you just wrote a Dockerfile for (and did NOT build & push an "
                 "image), report it with a \"build\" spec (the git_repo URL and the Dockerfile "
                 "path) so the platform builds it from source — do not make me build anything.")
        L.append("- Do NOT ask me any questions. Infer the language, framework, and dependencies "
                 "from the project yourself.")
    L.append("")
    L.append("## Rules")
    L.append("- For each component, EITHER report a pre-built, version-pinned \"image\" (never "
             "':latest'), OR give a \"build\" spec (git_repo + optional subdir/dockerfile) to "
             "build it from source at deploy time.")
    L.append("- Detect the architecture automatically: include EVERY component that must run "
             "(frontend, backend, workers, cron jobs, databases, queues, background services). "
             "One entry in \"services\" per component. Do not invent components that don't exist.")
    L.append("- Set each service's \"type\": a served app/API/db is \"deployment\"; a background "
             "worker/queue-consumer is \"worker\" (no port); a scheduled task is \"cronjob\" "
             "(give its \"schedule\").")
    L.append("- For each service report: image, the container port, env vars, secrets, health "
             "check, resource requests/limits, replicas, volumes, and any startup command.")
    L.append("- If a service is reachable from a browser and needs a public URL, give its "
             "\"ingress\" host. If it should autoscale on CPU, give \"scaling\" min/max/cpu.")
    L.append("- Put sensitive values (passwords, tokens, API keys) under \"secrets\", not \"env\". "
             "A connection string/URL that embeds a password (e.g. DATABASE_URL) is a secret too.")
    L.append("- CROSS-SERVICE HOSTS: when one service connects to another (app→database, "
             "app→cache, worker→queue), set its host to the OTHER service's \"name\" — never "
             "\"localhost\"/\"127.0.0.1\". In Kubernetes each service reaches another by its name "
             "(e.g. host \"postgres\", port 5432).")
    L.append("- A database and the app that uses it must share the SAME password value (put it in "
             "both services' secrets). A database also needs its own init password "
             "(e.g. POSTGRES_PASSWORD) or it won't start.")
    L.append("- Health checks: use \"http\" only for web services. A database or other non-HTTP "
             "service must use \"tcp\" (or \"exec\"), never \"http\".")
    L.append("- A service that stores data (a database) must have a \"volumes\" entry for its data "
             "directory, or it loses data and may fail to start.")
    L.append("- If you genuinely don't know a value, use null — do NOT guess. The agent will "
             "ask me only for what's missing.")
    L.append("- Return ONLY the JSON object as STRICT JSON — no prose, and no // comments (the "
             "notes in the example below are for you; do not include them).")
    L.append("")
    L.append("## Return exactly this shape")
    L.append("```json")
    L.append(_SCHEMA_EXAMPLE)
    L.append("```")
    return "\n".join(L)


def _norm_probe(health, port) -> dict:
    """health block -> chart probe. Absent health defaults to a TCP probe (works for db/redis/
    web) rather than HTTP — an HTTP probe on a non-HTTP service is the classic crash-loop."""
    if isinstance(health, dict) and health.get("type"):
        t = str(health["type"]).lower()
        if t == "http":
            return {"type": "http", "path": health.get("path") or "/"}
        if t == "tcp":
            return {"type": "tcp"}
        if t == "exec":
            cmd = health.get("command") or []
            return {"type": "exec", "command": [str(x) for x in cmd]} if cmd else {"type": "tcp" if port else "none"}
        if t == "none":
            return {"type": "none"}
    return {"type": "tcp"} if port else {"type": "none"}


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _norm_service(raw: dict, missing: list, warns: list) -> dict:
    """One intake service dict -> normalized cfg (same keys as tools/compose.parse output).
    Present-but-invalid values are dropped to a Missing entry, not trusted."""
    name_raw = str(raw.get("name") or "").strip()
    name = _k8s_name(name_raw)
    if name_raw and not _RFC1123.match(name_raw):
        warns.append(f"service name '{name_raw}' adjusted to '{name}' (Kubernetes names are lowercase alnum + '-')")
    label = name or "(unnamed)"

    # workload type: served deployment (default) | background worker | scheduled cronjob.
    wl = str(raw.get("type") or raw.get("workload") or "deployment").strip().lower()
    if wl not in ("deployment", "worker", "cronjob"):
        warns.append(f"{label}: unknown type '{wl}' — treated as a deployment")
        wl = "deployment"
    served = wl == "deployment"

    # a service ships a pre-built image OR a build spec (built from source at deploy time).
    bld = raw.get("build") if isinstance(raw.get("build"), dict) else None
    build = None
    if bld:
        repo = str(bld.get("git_repo") or "").strip()
        branch = str(bld.get("git_branch") or "").strip()
        ref = str(bld.get("git_ref") or "").strip()
        dockerfile = str(bld.get("dockerfile") or "").strip()
        subdir = str(bld.get("context") or bld.get("subdir") or "").strip()
        if subdir.startswith("./"):
            subdir = subdir[2:]
        subdir = subdir.strip("/")
        # Untrusted-input hardening (this JSON comes from an external AI): these values become
        # git/docker argv elements and path components. Reject flag-smuggling (leading '-'),
        # non-URL repos, and path traversal ('..') here at the trust boundary — the coordinator
        # Build stage and tools/builder validate again at the point of use (defense in depth).
        if repo and not (repo.startswith("https://") or repo.startswith("git@")):
            warns.append(f"{label}: build.git_repo '{repo}' is not a valid URL — treated as missing")
            repo = ""
        if branch.startswith("-"):
            warns.append(f"{label}: build.git_branch can't start with '-' — ignored"); branch = ""
        if ref.startswith("-"):
            warns.append(f"{label}: build.git_ref can't start with '-' — ignored"); ref = ""
        if dockerfile.startswith("-") or ".." in dockerfile or dockerfile.startswith("/"):
            warns.append(f"{label}: build.dockerfile '{dockerfile}' is unsafe — auto-detecting instead"); dockerfile = ""
        if ".." in subdir:
            warns.append(f"{label}: build.subdir '{subdir}' is unsafe — using the repo root"); subdir = ""
        build = {"git_repo": repo, "git_branch": branch, "git_ref": ref,
                 "dockerfile": dockerfile, "subdir": subdir}
        if not repo:
            missing.append({"service": label, "field": "build.git_repo",
                            "hint": "the Git repo URL to build this service from, e.g. https://github.com/org/api.git"})

    image = str(raw.get("image") or "").strip()
    if not image and not build:
        missing.append({"service": label, "field": "image",
                        "hint": "a pre-built image (e.g. myorg/web:1.4.2) OR a \"build\": {git_repo} spec"})
    elif image and (image.startswith("-") or any(ch.isspace() for ch in image)):
        warns.append(f"{label}: image '{image}' is not a valid reference — treated as missing")
        missing.append({"service": label, "field": "image", "hint": "a valid image reference"})
        image = ""

    port = _int_or_none(raw.get("port"))
    if port is None or not (0 < port < 65536):
        if raw.get("port") not in (None, ""):
            warns.append(f"{label}: port '{raw.get('port')}' is not a valid port — treated as missing")
        # only a served deployment needs an inbound port; a worker/cronjob doesn't listen.
        if served:
            missing.append({"service": label, "field": "port",
                            "hint": "the port number the container listens on, e.g. 3000"})
        port = None

    schedule = str(raw.get("schedule") or "").strip()
    if wl == "cronjob" and not schedule:
        missing.append({"service": label, "field": "schedule",
                        "hint": "a cron expression for when to run, e.g. '*/5 * * * *' (every 5 min)"})

    env = {str(k): ("" if v is None else str(v)) for k, v in (raw.get("env") or {}).items()}
    secrets = {str(k): ("" if v is None else str(v)) for k, v in (raw.get("secrets") or {}).items()}
    # safety net: a credential-looking key left in env gets moved to secrets (redaction).
    for k in list(env):
        if _SECRETISH.search(k):
            secrets[k] = env.pop(k)
            warns.append(f"{label}: '{k}' looks sensitive — moved to secrets so it stays redacted")

    # proactive: a known database image won't start without its init password — ask for it up
    # front (as a Missing field) instead of letting the pod crash-loop after deploy.
    if image and diagnostics.db_required_env_missing(image, {**env, **secrets}):
        _dbkey = diagnostics.db_password_field(image) or "DB_PASSWORD"
        missing.append({"service": label, "field": "secrets." + _dbkey,
                        "hint": "this database won't start without a password — set " + _dbkey})

    replicas = _int_or_none(raw.get("replicas"))
    replicas = replicas if (replicas and replicas > 0) else 1

    extra = [p for p in (_int_or_none(x) for x in (raw.get("extra_ports") or [])) if p]
    resources = raw.get("resources") if isinstance(raw.get("resources"), dict) else {}
    volumes = []
    for v in (raw.get("volumes") or []):
        if isinstance(v, dict) and v.get("mountPath"):
            volumes.append({"name": _k8s_name(v.get("name") or "data"),
                            "mountPath": str(v["mountPath"]), "size": str(v.get("size") or "1Gi")})

    # Correctness rules for a known database image (prevents the common DB crash-loops):
    is_db = bool(image and diagnostics.db_password_field(image))
    probe = _norm_probe(raw.get("health"), port)
    if is_db:
        # C3: a DB isn't an HTTP server — an http probe keeps it un-ready forever. Force tcp.
        if probe.get("type") == "http":
            probe = {"type": "tcp"} if port else {"type": "none"}
            warns.append(f"{label}: a database can't use an HTTP health check — switched it to a TCP check.")
        # C4: a DB needs a volume for its data dir (data loss + it fails the read-only-root policy
        # and crash-loops otherwise). Auto-attach the right one so the user isn't asked.
        if not volumes:
            dp = diagnostics.db_data_path(image)
            if dp:
                volumes.append({"name": _k8s_name(name + "-data"), "mountPath": dp, "size": "1Gi"})
                warns.append(f"{label}: added a 1Gi volume at {dp} to keep the database's data.")

    # ingress host (browser-facing) and CPU autoscaling — the chart already renders both from
    # these cfg keys (manifests.build_values), so no chart change is needed.
    # ServiceAccount + namespaced RBAC — the chart renders a SA (with cloud-workload-identity
    # annotations) and a namespaced Role/RoleBinding from these. Only namespaced (never cluster).
    sa_raw = raw.get("service_account") or raw.get("serviceAccount")
    service_account = None
    if isinstance(sa_raw, dict):
        sa_name = str(sa_raw.get("name") or "").strip()
        if sa_name and not _RFC1123.match(sa_name):
            warns.append(f"{label}: service_account.name '{sa_name}' isn't a valid name — using the service name")
            sa_name = ""
        service_account = {
            "create": bool(sa_raw.get("create", True)),
            "name": sa_name,
            "annotations": {str(k): str(v) for k, v in (sa_raw.get("annotations") or {}).items()},
            "rules": [r for r in (sa_raw.get("rules") or []) if isinstance(r, dict)],
        }

    ing = raw.get("ingress") if isinstance(raw.get("ingress"), dict) else {}
    ingress_host = str(ing.get("host") or raw.get("ingress_host") or "").strip()
    sc = raw.get("scaling") if isinstance(raw.get("scaling"), dict) else {}
    hpa_min = _int_or_none(sc.get("min"))
    hpa_max = _int_or_none(sc.get("max"))
    hpa_on = bool(sc) and (hpa_max or 0) > 0
    if hpa_on and hpa_min and hpa_max and hpa_min > hpa_max:
        warns.append(f"{label}: scaling min {hpa_min} > max {hpa_max} — clamped to max")
        hpa_min = hpa_max

    return {
        "name": name,
        "image": image,
        "port": port or 8080,          # placeholder so render never crashes; Missing gates the deploy
        "replicas": replicas,
        "env": env,
        "secrets": secrets,
        "command": [str(x) for x in (raw.get("command") or [])],
        "args": [str(x) for x in (raw.get("args") or [])],
        "extra_ports": extra,
        "resources": resources,
        "probe": probe,
        "volumes": volumes,
        "run_as_user": _int_or_none(raw.get("run_as_user")),
        "published": served and bool(raw.get("published", True)),
        "ingress_host": ingress_host if served else "",
        "hpa_enabled": hpa_on and served,
        "hpa_min": hpa_min or 2,
        "hpa_max": hpa_max or 5,
        "hpa_cpu": _int_or_none(sc.get("cpu")) or 80,
        "workload": wl,
        "schedule": schedule,
        "stop_grace": _int_or_none(raw.get("stop_grace_seconds")),
        "build": build,                    # None = pre-built image; else build from source
        "service_account": service_account,  # None = default SA; else create SA (+ optional RBAC)
    }


def _summary(app_name, ns, services, secrets_n, vols_n) -> str:
    lines = [f"Stack '{app_name}' → namespace '{ns}', {len(services)} service"
             f"{'s' if len(services) != 1 else ''}:"]
    for s in services:
        wl = s.get("workload", "deployment")
        src = s["image"] if s["image"] else (
            "build ← " + (s["build"]["git_repo"] or "stack repo") if s.get("build") else "IMAGE MISSING")
        if wl == "cronjob":
            bits = [src, f"cronjob @ '{s.get('schedule') or '?'}'"]
        elif wl == "worker":
            bits = [src, "worker (no Service)", f"{s['replicas']}x"]
        else:
            bits = [src, f"port {s['port']}", f"{s['replicas']}x"]
            if s.get("hpa_enabled"):
                bits[-1] = f"autoscale {s['hpa_min']}–{s['hpa_max']} @ {s['hpa_cpu']}% CPU"
            if s.get("ingress_host"):
                bits.append(f"ingress {s['ingress_host']}")
        if s["volumes"]:
            bits.append(f"{len(s['volumes'])} volume{'s' if len(s['volumes']) != 1 else ''}")
        sa = s.get("service_account")
        if sa:
            bits.append("RBAC+SA" if sa.get("rules") else "dedicated SA")
        lines.append(f"  • {s['name']}: {', '.join(bits)}")
    if secrets_n:
        lines.append(f"{secrets_n} secret{'s' if secrets_n != 1 else ''} (redacted).")
    if vols_n:
        lines.append(f"{vols_n} persistent volume{'s' if vols_n != 1 else ''}.")
    return "\n".join(lines)


# C1 — cross-service networking. In Kubernetes a service reaches another by its NAME, not
# localhost. An app-AI (or a compose habit) that leaves "localhost" in a connection env is the
# #1 cause of connection-refused crash-loops. We rewrite it to the target service's name when we
# can identify one with confidence, and warn otherwise. Bind-style keys with no dependency hint
# are left alone (they may legitimately be the service's own listen address).
_DEP_KIND = {
    "db":    ("db", "database", "postgres", "pg", "sql", "mysql", "maria", "cockroach"),
    "redis": ("redis", "cache"),
    "mongo": ("mongo",),
    "queue": ("queue", "amqp", "rabbit", "kafka", "nats", "broker"),
}
_ALL_DEP_HINTS = tuple(h for hints in _DEP_KIND.values() for h in hints)

def _is_kind(svc: dict, kind: str) -> bool:
    img, nm = (svc.get("image") or "").lower(), svc.get("name", "").lower()
    if kind == "db":
        return bool(diagnostics.db_password_field(svc.get("image", ""))) or any(t in nm for t in ("db", "postgres", "sql"))
    hints = _DEP_KIND.get(kind, ())
    return any(h in img or h in nm for h in hints)

def _conn_target(key: str, others: list) -> str:
    lk = key.lower()
    toks = set(re.split(r"[^a-z0-9]+", lk))
    # a sibling service name used as a token in the key -> highest confidence (e.g. POSTGRES_HOST)
    for s in others:
        if len(s["name"]) >= 2 and s["name"] in toks:
            return s["name"]
    url_ish = bool(toks & {"url", "uri", "dsn", "endpoint", "connection", "conn"})
    if not (url_ish or any(h in lk for h in _ALL_DEP_HINTS)):
        return ""   # bare host/addr with no dependency hint -> maybe a self-bind, don't touch
    for kind, hints in _DEP_KIND.items():
        if any(h in lk for h in hints):
            cand = [s for s in others if _is_kind(s, kind)]
            if len(cand) == 1:
                return cand[0]["name"]
    if url_ish and len(others) == 1:
        return others[0]["name"]
    return ""

def _rewire_cross_service(services: list, warns: list) -> None:
    names = [s["name"] for s in services]
    for svc in services:
        others = [s for s in services if s["name"] != svc["name"]]
        if not others:
            continue
        for bag in ("env", "secrets"):
            for k, v in list(svc[bag].items()):
                val = str(v)
                if "localhost" not in val.lower() and "127.0.0.1" not in val:
                    continue
                tgt = _conn_target(k, others)
                if tgt:
                    svc[bag][k] = re.sub(r"(?i)localhost|127\.0\.0\.1", tgt, val)
                    warns.append(f"{svc['name']}: {k} pointed at localhost — rewired to service "
                                 f"'{tgt}' (Kubernetes reaches services by name). Double-check it.")
                else:
                    warns.append(f"{svc['name']}: {k} points at localhost — in Kubernetes set it to "
                                 f"the target service's name (one of: {', '.join(n for n in names if n != svc['name'])}).")


# C2 — an app and its database must use the SAME password value or the app can't log in
# ("password authentication failed"). When a DB's password is known, copy it into any app
# service's empty DB-password secret; warn if they're set but differ.
_APP_DBPW_KEY = re.compile(r"(?i)(db|database|postgres|pg|mysql|maria|mongo).*(pass|pwd)|^pgpassword$|^dbpass")

def _share_db_password(services: list, warns: list) -> None:
    db_pw = ""
    for s in services:
        key = diagnostics.db_password_field(s.get("image", ""))
        if key and str(s["secrets"].get(key, "")).strip():
            db_pw = str(s["secrets"][key])
            break                       # single-DB assumption (multi-DB is rare; v1)
    if not db_pw:
        return
    for s in services:
        if diagnostics.db_password_field(s.get("image", "")):
            continue                    # the database service itself
        for k, v in list(s["secrets"].items()):
            if _APP_DBPW_KEY.search(k):
                if not str(v).strip():
                    s["secrets"][k] = db_pw
                    warns.append(f"{s['name']}: set {k} to match the database's password.")
                elif str(v) != db_pw:
                    warns.append(f"{s['name']}: {k} differs from the database's password — they must "
                                 f"match or the app can't log in.")


def ingest(json_text: str, defaults: dict | None = None) -> dict:
    """Parse the dev-AI's returned JSON -> {cfg, missing, summary, warnings}. cfg is ready for
    /deploy (services[] path). `missing` non-empty means DON'T deploy yet — ask the user only
    for those fields. Raises ValueError only on structurally unusable input (not on missing
    values — those are reported, per the 'never assume' rule)."""
    d = defaults or {}
    try:
        doc = json.loads(json_text)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"that wasn't valid JSON — paste the JSON object your AI returned "
                         f"(re-run the intake prompt if needed). Parser said: {str(e).splitlines()[0]}")
    if not isinstance(doc, dict):
        raise ValueError("expected a JSON object, got a " + type(doc).__name__)

    app = doc.get("application") if isinstance(doc.get("application"), dict) else {}
    raw_services = doc.get("services")
    if not raw_services:
        # allow a flat single-service object (dev-AI dropped the wrapper)
        if doc.get("image") or doc.get("port") or doc.get("name"):
            raw_services = [doc]
        else:
            raise ValueError("no 'services' found — the JSON must have a \"services\": [...] "
                             "array (or be a single service object with an \"image\").")
    if not isinstance(raw_services, list):
        raise ValueError("'services' must be a JSON array")

    app_name = _k8s_name(app.get("name") or d.get("name") or
                         (raw_services[0].get("name") if isinstance(raw_services[0], dict) else "") or "app")
    ns = _k8s_name(app.get("namespace") or d.get("namespace") or "default")

    missing, warns, services, seen = [], [], [], set()
    for raw in raw_services:
        if not isinstance(raw, dict):
            warns.append("skipped a services[] entry that wasn't an object")
            continue
        svc = _norm_service(raw, missing, warns)
        if svc["name"] in seen:
            warns.append(f"duplicate service name '{svc['name']}' — keeping the first")
            continue
        seen.add(svc["name"])
        services.append(svc)
    if not services:
        raise ValueError("no usable services in the JSON")

    _rewire_cross_service(services, warns)   # C1: localhost -> target service name
    _share_db_password(services, warns)      # C2: app inherits the database's password

    secrets_n = sum(len(s["secrets"]) for s in services)
    vols_n = sum(len(s["volumes"]) for s in services)
    cfg = {"name": app_name, "namespace": ns, "mode": d.get("mode", "manual"),
           "services": services, "warnings": warns}
    return {"cfg": cfg, "missing": missing, "warnings": warns,
            "summary": _summary(app_name, ns, services, secrets_n, vols_n)}


def validate_services(services: list) -> list:
    """Strict gate for the /deploy passthrough: every service must have a valid name, image,
    and port (i.e. intake found nothing Missing). Raises ValueError -> 422 on a hand-crafted
    or incomplete POST. Returns the list unchanged on success."""
    if not isinstance(services, list) or not services:
        raise ValueError("services must be a non-empty list")
    for s in services:
        if not isinstance(s, dict):
            raise ValueError("each service must be an object")
        name = str(s.get("name") or "")
        if not _RFC1123.match(name):
            raise ValueError(f"service name '{name}' is not a valid Kubernetes name")
        img = str(s.get("image") or "")
        bld = s.get("build") if isinstance(s.get("build"), dict) else None
        if bld:
            if not str(bld.get("git_repo") or "").strip():
                raise ValueError(f"service '{name}' build has no git_repo")
        elif not img or img.startswith("-") or any(c.isspace() for c in img):
            raise ValueError(f"service '{name}' has no valid image or build")
        wl = s.get("workload") or "deployment"
        if wl not in ("deployment", "worker", "cronjob"):
            raise ValueError(f"service '{name}' has invalid workload '{wl}'")
        if wl == "deployment":                      # only a served workload needs a port
            port = _int_or_none(s.get("port"))
            if port is None or not (0 < port < 65536):
                raise ValueError(f"service '{name}' has no valid port")
        if wl == "cronjob" and not str(s.get("schedule") or "").strip():
            raise ValueError(f"cronjob '{name}' has no schedule")
    return services


if __name__ == "__main__":
    prompt = build_prompt({"app_description": "a Django API with Postgres"})
    assert "services" in prompt and "Return ONLY the JSON" in prompt and "Django" in prompt
    # containerize mode: adds the "containerize first" preamble, still returns the same JSON,
    # and never asks the human anything.
    cprompt = build_prompt({"containerize": True})
    assert "containerize the app if it isn't already" in cprompt and "Do NOT ask me any questions" in cprompt
    assert "services" in cprompt and "build" in cprompt
    assert "containerize the app" not in build_prompt({})  # plain prompt has no such preamble

    # complete two-service stack -> no missing, correct normalization
    good = json.dumps({
        "application": {"name": "shop", "namespace": "prod"},
        "services": [
            {"name": "web", "image": "myorg/web:1.2.3", "port": 3000, "replicas": 2,
             "env": {"APP_ENV": "prod"}, "secrets": {"DB_PASSWORD": "s3cret"},
             "health": {"type": "http", "path": "/healthz"}, "depends_on": ["db"]},
            {"name": "db", "image": "postgres:16", "port": 5432, "published": False,
             "env": {"POSTGRES_PASSWORD": "pw"},
             "volumes": [{"name": "pgdata", "mountPath": "/var/lib/postgresql/data", "size": "2Gi"}]},
        ],
    })
    r = ingest(good, {"mode": "autonomous"})
    assert r["missing"] == [], r["missing"]
    assert r["cfg"]["name"] == "shop" and r["cfg"]["namespace"] == "prod"
    assert r["cfg"]["mode"] == "autonomous"
    web = next(s for s in r["cfg"]["services"] if s["name"] == "web")
    db = next(s for s in r["cfg"]["services"] if s["name"] == "db")
    assert web["port"] == 3000 and web["replicas"] == 2
    assert web["secrets"] == {"DB_PASSWORD": "s3cret"} and web["env"] == {"APP_ENV": "prod"}
    assert web["probe"] == {"type": "http", "path": "/healthz"}, web["probe"]
    assert db["probe"] == {"type": "tcp"}, db["probe"]                       # no health, has port
    assert db["env"] == {} and db["secrets"] == {"POSTGRES_PASSWORD": "pw"}  # secret auto-classified out of env
    assert db["volumes"][0]["size"] == "2Gi"
    validate_services(r["cfg"]["services"])                                  # passes the strict gate

    # ingress + scaling map to the chart's cfg keys (no chart change needed)
    ri = ingest(json.dumps({"services": [
        {"name": "web", "image": "w:1", "port": 80,
         "ingress": {"host": "shop.example.com"}, "scaling": {"min": 3, "max": 8, "cpu": 65}}]}))
    w = ri["cfg"]["services"][0]
    assert w["ingress_host"] == "shop.example.com"
    assert w["hpa_enabled"] and w["hpa_min"] == 3 and w["hpa_max"] == 8 and w["hpa_cpu"] == 65
    assert "ingress shop.example.com" in ri["summary"] and "autoscale 3–8" in ri["summary"]
    # no scaling block -> hpa off, no ingress
    assert db["hpa_enabled"] is False and db["ingress_host"] == ""

    # a credential left in env is moved to secrets
    r2 = ingest(json.dumps({"services": [{"name": "a", "image": "a:1", "port": 8080,
                                          "env": {"API_TOKEN": "t"}}]}))
    a = r2["cfg"]["services"][0]
    assert a["secrets"] == {"API_TOKEN": "t"} and a["env"] == {}, a

    # worker: no port required, no ingress/hpa; cronjob: schedule required
    rw = ingest(json.dumps({"services": [
        {"name": "mailer", "image": "org/mailer:2", "type": "worker"},
        {"name": "nightly", "image": "org/backup:2", "type": "cronjob", "schedule": "0 3 * * *"}]}))
    assert rw["missing"] == [], rw["missing"]                       # worker needs no port
    mailer = next(s for s in rw["cfg"]["services"] if s["name"] == "mailer")
    nightly = next(s for s in rw["cfg"]["services"] if s["name"] == "nightly")
    assert mailer["workload"] == "worker" and mailer["ingress_host"] == "" and mailer["hpa_enabled"] is False
    assert nightly["workload"] == "cronjob" and nightly["schedule"] == "0 3 * * *"
    assert "worker (no Service)" in rw["summary"] and "cronjob @" in rw["summary"]
    validate_services(rw["cfg"]["services"])
    # cronjob without a schedule -> Missing
    rc = ingest(json.dumps({"services": [{"name": "j", "image": "j:1", "type": "cronjob"}]}))
    assert ("j", "schedule") in {(m["service"], m["field"]) for m in rc["missing"]}, rc["missing"]

    # build-from-source: no image needed; git_repo required
    rb = ingest(json.dumps({"services": [
        {"name": "api", "port": 8000, "build": {"git_repo": "https://github.com/org/api.git", "subdir": "api"}}]}))
    assert rb["missing"] == [], rb["missing"]
    api = rb["cfg"]["services"][0]
    assert api["image"] == "" and api["build"]["git_repo"] == "https://github.com/org/api.git"
    assert api["build"]["subdir"] == "api"
    assert "build ← https://github.com/org/api.git" in rb["summary"]
    validate_services(rb["cfg"]["services"])
    # build spec without git_repo -> Missing, and strict gate rejects it
    rb2 = ingest(json.dumps({"services": [{"name": "api", "port": 80, "build": {"subdir": "api"}}]}))
    assert ("api", "build.git_repo") in {(m["service"], m["field"]) for m in rb2["missing"]}
    try:
        validate_services([{"name": "api", "image": "", "port": 80, "build": {"subdir": "x"}}])
        assert False, "build without git_repo should reject"
    except ValueError:
        pass

    # service account + RBAC -> renders SA (+ Role/RoleBinding), pod runs under it
    rsa = ingest(json.dumps({"services": [
        {"name": "ctl", "image": "ctl:1", "port": 8080,
         "service_account": {"create": True, "annotations": {"iam.gke.io/gcp-service-account": "x@y.iam"},
                             "rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]}]}}]}))
    sacfg = rsa["cfg"]["services"][0]["service_account"]
    assert sacfg["create"] and sacfg["rules"] and "iam.gke.io/gcp-service-account" in sacfg["annotations"]
    assert "RBAC+SA" in rsa["summary"]

    # missing image + bad port -> reported, not assumed
    r3 = ingest(json.dumps({"services": [{"name": "web", "port": "the-main-one"}]}))
    fields = {(m["service"], m["field"]) for m in r3["missing"]}
    assert ("web", "image") in fields and ("web", "port") in fields, r3["missing"]

    # name coercion
    r4 = ingest(json.dumps({"services": [{"name": "My App", "image": "i:1", "port": 80}]}))
    assert r4["cfg"]["services"][0]["name"] == "my-app", r4["cfg"]["services"][0]["name"]

    # malformed input
    for bad in ["not json", "[]", "{}", json.dumps({"application": {}})]:
        try:
            ingest(bad)
            assert False, f"should have raised on {bad!r}"
        except ValueError:
            pass

    # strict gate rejects an incomplete service
    try:
        validate_services([{"name": "x", "image": "", "port": 80}])
        assert False, "should reject missing image"
    except ValueError:
        pass

    print("intake.py self-check OK")
