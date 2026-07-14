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
import urllib.parse

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
  "application": { "name": "", "namespace": "" },
  "services": [
    {
      "name": "",
      "type": "deployment | worker | cronjob",
      "image": "",
      "port": 0,
      "schedule": "",
      "replicas": 1,
      "max_safe_replicas": 1,
      "replica_constraint_reason": "",
      "published": false,
      "env": { "KEY": { "value": "", "required_to_boot": false } },
      "secrets": { "KEY": { "value": null, "required_to_boot": true } },
      "connects_to": [
        { "service": "", "hostname_in_code": "", "port": 0, "protocol": "",
          "from": "server | browser",
          "path_prefix": "",
          "browser_base": { "env": "", "value": "", "baked_at_build": false } }
      ],
      "database_init": { "strategy": "on_startup | init_job | none", "command": [] },
      "command": [],
      "args": [],
      "resources": {
        "requests": { "cpu": "", "memory": "" },
        "limits": { "cpu": "", "memory": "" }
      },
      "health": { "type": "http | tcp | exec | none", "path": "", "command": [] },
      "volumes": [ { "name": "", "mountPath": "", "size": "" } ],
      "run_as_user": null,
      "stop_grace_seconds": 30,
      "needs_outbound_internet": false,
      "uses_websockets": false,
      "ingress": { "host": null },
      "build": { "git_repo": "", "git_branch": "main", "subdir": "", "dockerfile": "" },
      "depends_on": []
    }
  ],
  "smoke_tests": [
    { "via": "<published service name>", "path": "/", "expect_status": 200, "proves": "UI serves" }
  ]
}"""

# CHANGE 1 — the "Describe Your Application" prompt, verbatim per the upgrade spec. Deterministic
# (no LLM). {{BOT_NAME}} is filled by build_prompt.
_APP_DESC_PROMPT = """You are the AI that built (or fully understands) this application. A
deployment bot ({{BOT_NAME}}) will deploy it to Kubernetes from prebuilt container images WITHOUT
reading any code. The bot only consumes the JSON you produce. Answer from your knowledge of the
codebase — do not guess. If you genuinely do not know a value, use null.

Return ONE strict JSON object (no prose, no comments) in the schema below. Rules you must follow
while filling it:

1. ARCHITECTURE: include every component that must run (frontend, backend, databases, workers,
   cron jobs). Do not invent components.
2. IMAGES: version-pinned tags only, never ":latest". (Or omit "image" and give a "build" spec
   with the git_repo so the bot builds it from source.)
3. CONNECTION WIRING (the most common running-but-broken cause): for EVERY connection one part
   of the app makes to another, add a "connects_to" entry, and state where the call ORIGINATES:
   - "from": "server"  — one container calls another INSIDE the cluster (backend→database,
     worker→queue, server-side rendering). Report the EXACT hostname string used in the
     code/config as "hostname_in_code" (e.g. a DB URL host, an nginx proxy_pass host). The bot
     names the Kubernetes Service to match, so cluster DNS resolves it.
   - "from": "browser" — code running in the USER'S BROWSER calls a backend (a frontend's
     fetch/axios/XHR to an API). CRITICAL: the browser CANNOT resolve Kubernetes service names or
     "localhost" — it can only reach the app through its public URL. So a browser→backend call
     MUST use a SAME-ORIGIN RELATIVE path (e.g. "/api"), not an absolute URL and not a service
     name. For each browser connection report "path_prefix" (the path the frontend calls, e.g.
     "/api") and "browser_base": the env var / config key the frontend reads for its API base,
     its current value, and whether that value is BAKED AT BUILD TIME (compiled into the JS
     bundle — it CANNOT be changed at deploy) or read at RUNTIME.
   If the frontend currently hardcodes an absolute URL, "localhost", or a cluster service name
   for a BROWSER call, FIX the frontend to call a same-origin relative path (e.g. "/api") that is
   runtime-configurable, and report the "path_prefix". The bot will route that path to the
   backend via one ingress, so frontend and backend share one origin (no CORS, no broken host).
4. SECRETS: passwords, tokens, API keys, and any URL embedding a password go under "secrets",
   never "env". A database and every consumer of it must share the same password value. Passwords
   you generate must be URL-safe (letters/digits only). For each env var, set "required_to_boot"
   honestly: which missing values crash the app vs. merely disable a feature.
5. DATABASE INIT: state exactly how the schema gets created ("on_startup" if the image's own
   start command runs migrations, "init_job" + the command if the bot must run it once, or
   "none"). An app whose tables are never created deploys green and fails on first query.
6. REPLICA SAFETY: for each service state "max_safe_replicas" and why. If the service holds
   in-memory state, singletons, WebSocket engines, or background loops, it is 1 — say so even if
   it looks stateless.
7. HEALTH: "http" + path only for web services; databases and non-HTTP services get "tcp" or
   "exec". Every service that stores data gets a "volumes" entry for its data directory.
8. USERS: report the numeric UID the image runs as ("run_as_user"). Kubernetes cannot verify
   named users for runAsNonRoot.
9. SMOKE TESTS (critical): provide 2-5 HTTP checks that prove the WHOLE app works end-to-end
   through the browser-facing entrypoint — at least one static/UI path, one API path, and one
   path that exercises the database. These are the bot's definition of "deployment succeeded";
   "pods Running" is not success.
10. Also report: outbound internet needs, WebSocket usage, graceful shutdown seconds, and rough
    memory appetite (does anything load an ML model or large dataset?).

SCHEMA:
""" + _SCHEMA_EXAMPLE


def build_prompt(context: dict | None = None) -> str:
    """The 'Describe Your Application' prompt the developer relays to the AI that built their app.
    Deterministic (no LLM) — the schema is fixed. context.containerize=True prepends a preamble
    telling that AI to containerize the app first when it isn't already."""
    c = context or {}
    prompt = _APP_DESC_PROMPT.replace("{{BOT_NAME}}", "Helmsman")
    if c.get("containerize"):
        pre = ("## First — containerize the app if it isn't already\n"
               "- If it already has working image(s)/Dockerfile(s), report them.\n"
               "- If NOT: detect the architecture and write a production-grade, multi-stage "
               "Dockerfile for each component (minimal pinned base, non-root, .dockerignore) and "
               "commit them. Report such a component with a \"build\" spec (git_repo + dockerfile) "
               "so the bot builds it from source — do not make me build anything.\n"
               "- Do NOT ask me any questions; infer language/framework/dependencies yourself.\n\n")
        prompt = pre + prompt
    return prompt


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


def _norm_kv(d) -> tuple[dict, set]:
    """Normalize env/secrets that may be rich ({KEY:{value,required_to_boot}}) or flat ({KEY:val}).
    Returns (flat {KEY:str}, required_keys) — required_keys are those with required_to_boot=true."""
    flat, required = {}, set()
    for k, v in (d or {}).items():
        k = str(k)
        if isinstance(v, dict) and ("value" in v or "required_to_boot" in v):
            val = v.get("value")
            flat[k] = "" if val is None else str(val)
            if v.get("required_to_boot"):
                required.add(k)
        else:
            flat[k] = "" if v is None else str(v)
    return flat, required


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

    # env/secrets accept BOTH the rich form {KEY:{value,required_to_boot}} and the flat form
    # {KEY:val} (backward compatible). We flatten for the chart and keep the required-to-boot keys.
    env, req_env = _norm_kv(raw.get("env"))
    secrets, req_sec = _norm_kv(raw.get("secrets"))
    # safety net: a credential-looking key left in env gets moved to secrets (redaction).
    for k in list(env):
        if _SECRETISH.search(k):
            secrets[k] = env.pop(k)
            if k in req_env:
                req_env.discard(k); req_sec.add(k)
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

    # new upgrade-spec metadata (used by validation + the new deploy/verify stages)
    connects_to = [c for c in (raw.get("connects_to") or []) if isinstance(c, dict)]
    di = raw.get("database_init") if isinstance(raw.get("database_init"), dict) else {}
    db_init = {"strategy": str(di.get("strategy") or "").strip().lower() or None,
               "command": [str(x) for x in (di.get("command") or [])]}
    msr = _int_or_none(raw.get("max_safe_replicas"))

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
        # --- upgrade-spec metadata (validation + new stages) ---
        "required_env": sorted(req_env),
        "required_secrets": sorted(req_sec),
        "connects_to": connects_to,
        "database_init": db_init,
        "max_safe_replicas": msr,
        "replica_constraint_reason": str(raw.get("replica_constraint_reason") or "").strip(),
        "uses_websockets": bool(raw.get("uses_websockets")),
        "needs_outbound_internet": bool(raw.get("needs_outbound_internet")),
        "is_db": is_db,
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


_URLSAFE_PW = re.compile(r"^[A-Za-z0-9_-]*$")

def validate_manifest(services: list, smoke_tests: list, warns: list) -> dict:
    """CHANGE 2 — reject a manifest with per-field errors BEFORE anything deploys. Returns
    {errors: [str], questions: [{service,field,hint}]}. `errors` block the deploy; `questions`
    (null/empty required values) become the ONE consolidated question list for the user."""
    errors, questions = [], []
    names = {s["name"] for s in services}

    # DB password per DB service (for rule c: consumer URL password must match)
    db_pw = {}
    for s in services:
        key = diagnostics.db_password_field(s.get("image", ""))
        if key:
            db_pw[s.get("name", "")] = str((s.get("secrets") or {}).get(key, ""))

    def q(service, field, hint):
        questions.append({"service": service, "field": field, "hint": hint})

    for s in services:
        nm = s.get("name", "")
        s_env, s_secrets = (s.get("env") or {}), (s.get("secrets") or {})
        # (a) required env/secret empty or null — check LENGTH, not just presence
        for k in (s.get("required_secrets") or []):
            if len(str(s_secrets.get(k, "")).strip()) == 0:
                q(nm, "secrets." + k, f"{nm} won't boot without secret {k} — provide a value")
        for k in (s.get("required_env") or []):
            if len(str(s_env.get(k, "")).strip()) == 0:
                q(nm, "env." + k, f"{nm} won't boot without env {k} — provide a value")
        # (i) image tag latest/missing (a build spec is exempt — it produces a pinned tag)
        img = s.get("image", "")
        if not s.get("build"):
            tag = img.rsplit(":", 1)[-1] if ":" in img.split("/")[-1] else ""
            if not img:
                errors.append(f"{nm}: no image and no build spec")
            elif tag in ("", "latest"):
                errors.append(f"{nm}: image '{img}' must be version-pinned (never ':latest' or untagged)")
        # (e) a data-storing / non-HTTP service must not use an http probe
        if (s.get("is_db") or s.get("volumes")) and (s.get("probe") or {}).get("type") == "http":
            errors.append(f"{nm}: a data-storing service must use a tcp/exec health check, not http")
        # (f) a data-storing service with no volume (C4 usually auto-adds; this catches the rest)
        if s.get("is_db") and not s.get("volumes"):
            q(nm, "volumes", f"{nm} stores data but has no volume — give a data dir + size")
        # (h) requested replicas exceed the app author's safe maximum -> hard error
        msr = s.get("max_safe_replicas")
        if msr is not None and s.get("replicas", 1) > msr:
            why = f" ({s['replica_constraint_reason']})" if s.get("replica_constraint_reason") else ""
            errors.append(f"{nm}: requested {s['replicas']} replicas but max_safe_replicas={msr}{why}")
        # (d) HOSTNAME CONTRACT: every connects_to hostname must be a service name
        for c in s.get("connects_to", []):
            host = str(c.get("hostname_in_code") or "").strip()
            if host and host not in names:
                errors.append(f"{nm}: connects to host '{host}', which is not a service in this "
                              f"manifest — rename a service to '{host}' or reconfigure the image; "
                              f"Kubernetes DNS will not resolve this name.")
        # (b) a URL with a password: must parse, and the password must be URL-safe
        for bag, bagd in (("env", s_env), ("secrets", s_secrets)):
            for k, v in bagd.items():
                val = str(v)
                if "://" not in val:
                    continue
                try:
                    u = urllib.parse.urlparse(val)
                except Exception:
                    errors.append(f"{nm}: {k} looks like a URL but does not parse")
                    continue
                if u.password and not _URLSAFE_PW.match(u.password):
                    errors.append(f"{nm}: the password embedded in {k} has characters outside "
                                  f"[A-Za-z0-9_-] — use a URL-safe password or URL-encode it.")
                # (c) the URL's password must match the target DB's init password
                tgt = u.hostname or ""
                if tgt in db_pw and u.password and db_pw[tgt] and u.password != db_pw[tgt]:
                    errors.append(f"{nm}: the DB password in {k} differs from {tgt}'s init password "
                                  f"— they must match.")
        # (g) runAsNonRoot needs a numeric UID; a named/missing user can't be verified
        if s.get("run_as_user") is None and not s.get("is_db") and not s.get("build"):
            warns.append(f"{nm}: no numeric run_as_user given — if the image runs as a named user, "
                         f"Kubernetes' runAsNonRoot check may reject it. Confirm the numeric UID.")

    # (j) smoke tests: must exist and at least one should exercise the database
    if not smoke_tests:
        warns.append("no smoke_tests provided — 'pods Running' will be treated as success, which "
                     "can hide a running-but-broken app. Add 2-5 HTTP checks.")
    elif not any(re.search(r"(?i)(db|database|query|data|order|user|record|api)", str(t.get("proves", "")) + str(t.get("path", "")))
                 for t in smoke_tests):
        warns.append("no smoke_test appears to exercise the database — add one that does a real "
                     "read/write so a broken DB connection is caught.")

    return {"errors": errors, "questions": questions}


def _wire_browser_routes(services: list, warns: list) -> list:
    """CONNECTION WIRING — a browser→backend call can't use a cluster service name (the browser
    can't resolve it). Route the frontend's path prefix (e.g. /api) to the backend on ONE ingress
    (same origin), and inject that relative base into the frontend at runtime when possible.
    Returns a list of 'healing' notes for connections that can't be auto-fixed (baked-at-build)."""
    by_name = {s["name"]: s for s in services}
    heal = []
    for s in services:
        routes = []
        for c in (s.get("connects_to") or []):
            if str(c.get("from", "")).lower() != "browser":
                continue
            tgt = str(c.get("service") or c.get("hostname_in_code") or "").strip()
            target = by_name.get(tgt)
            prefix = "/" + (str(c.get("path_prefix") or "/api").strip().lstrip("/"))
            port = _int_or_none(c.get("port")) or (target.get("port") if target else 8080)
            if not target:
                warns.append(f"{s['name']}: browser call to '{tgt}' — no such service to route to.")
                continue
            routes.append({"path": prefix, "service": tgt, "port": port})
            bb = c.get("browser_base") if isinstance(c.get("browser_base"), dict) else {}
            envk = str(bb.get("env") or "").strip()
            if envk and not bb.get("baked_at_build"):
                s.setdefault("env", {})[envk] = prefix   # frontend calls the same-origin relative path
                warns.append(f"{s['name']}: set {envk}={prefix} (same-origin) so the browser reaches "
                             f"'{tgt}' through the ingress.")
            elif bb.get("baked_at_build"):
                heal.append({"service": s["name"], "target": tgt, "path_prefix": prefix,
                             "problem": "the frontend's API base is baked into the build — it can't be "
                                        "changed at deploy; it must be rebuilt to call " + prefix})
            else:
                # no runtime env to inject: we can route the path, but can't guarantee the frontend
                # calls it — the code must use the relative prefix. Flag for the healing prompt.
                heal.append({"service": s["name"], "target": tgt, "path_prefix": prefix,
                             "problem": "no runtime-configurable API base was reported, so I can't "
                                        "point the frontend at " + prefix + " automatically — the "
                                        "frontend code must call that relative path itself"})
        if routes:
            s["ingress_routes"] = routes
            if not s.get("ingress_host"):
                warns.append(f"{s['name']}: to route {', '.join(r['path'] for r in routes)} to the "
                             f"backend from the browser, this service needs an ingress host. On a local "
                             f"cluster without an ingress controller the frontend must proxy those paths "
                             f"itself (see the healing prompt).")
    return heal


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

    _rewire_cross_service(services, warns)   # C1: localhost -> target service name (server-side)
    _share_db_password(services, warns)      # C2: app inherits the database's password
    heal = _wire_browser_routes(services, warns)   # browser→backend: ingress path routing + base

    smoke_tests = [t for t in (doc.get("smoke_tests") or []) if isinstance(t, dict)]
    val = validate_manifest(services, smoke_tests, warns)   # CHANGE 2: intake validation
    missing += val["questions"]              # consolidate nulls/empties into ONE question list

    secrets_n = sum(len(s["secrets"]) for s in services)
    vols_n = sum(len(s["volumes"]) for s in services)
    cfg = {"name": app_name, "namespace": ns, "mode": d.get("mode", "manual"),
           "services": services, "warnings": warns, "smoke_tests": smoke_tests}
    return {"cfg": cfg, "missing": missing, "errors": val["errors"], "warnings": warns,
            "smoke_tests": smoke_tests,
            "healing_prompt": build_healing_prompt(heal) if heal else "",
            "summary": _summary(app_name, ns, services, secrets_n, vols_n)}


def build_healing_prompt(heal: list) -> str:
    """A copy-paste prompt for the app's AI to FIX a frontend that can't reach its backend from the
    browser (e.g. an API base baked into the build). It explains the constraint and the fix, and
    asks the AI to change the code and return the corrected manifest."""
    L = []
    L.append("The deployment bot found a wiring problem it cannot fix at deploy time — your "
             "frontend calls its backend from the BROWSER, but the way the URL is configured won't "
             "work once deployed. Fix it in the code and return the corrected deployment JSON.")
    L.append("")
    L.append("## Why (Kubernetes networking)")
    L.append("- Code running in the browser CANNOT resolve Kubernetes service names (e.g. "
             "\"backend\") or \"localhost\" — those only work server-to-server inside the cluster.")
    L.append("- The frontend must reach the backend through a SAME-ORIGIN relative path (e.g. "
             "\"/api\") that the bot routes to the backend via one ingress. No absolute URL, no "
             "service name, no localhost, in browser code.")
    L.append("")
    L.append("## What to change")
    for h in heal:
        L.append(f"- Service \"{h['service']}\" calls \"{h['target']}\": {h['problem']}")
        L.append(f"  Make the frontend call the relative path \"{h['path_prefix']}\" and, if the API "
                 f"base is baked at build time, either read it at RUNTIME (from window/config or an "
                 f"env injected into a small config.js) or rebuild the image with the base set to "
                 f"\"{h['path_prefix']}\". Report the value you used.")
    L.append("")
    L.append("## Then")
    L.append("Return the SAME deployment JSON as before, corrected: each browser connection's "
             "\"browser_base\" should be a relative path and \"baked_at_build\": false (or provide "
             "a \"build\" spec so the bot rebuilds it with the right base).")
    return "\n".join(L)


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
    # CHANGE 2 hard errors (latest tag, hostname-contract mismatch, replicas > max_safe, ...):
    # re-check at the deploy gate so a hand-crafted POST can't skip intake validation.
    errs = validate_manifest(services, [], [])["errors"]
    if errs:
        raise ValueError("; ".join(errs))
    return services


if __name__ == "__main__":
    prompt = build_prompt()
    assert "connects_to" in prompt and "smoke_tests" in prompt and "Return ONE strict JSON" in prompt
    assert "CONNECTION WIRING" in prompt and "required_to_boot" in prompt and "Helmsman" in prompt
    assert "from\": \"server | browser" in prompt and "browser_base" in prompt

    # browser→backend wiring: /api routed to the backend, relative base injected, no healing needed
    bw = ingest(json.dumps({"application": {"name": "shop"}, "services": [
        {"name": "web", "image": "org/web:1", "port": 80, "published": True, "ingress": {"host": "shop.example.com"},
         "connects_to": [{"service": "api", "from": "browser", "path_prefix": "/api", "port": 8000,
                          "browser_base": {"env": "VITE_API_URL", "baked_at_build": False}}]},
        {"name": "api", "image": "org/api:1", "port": 8000}]}))
    web_b = next(s for s in bw["cfg"]["services"] if s["name"] == "web")
    assert web_b["ingress_routes"] == [{"path": "/api", "service": "api", "port": 8000}]
    assert web_b["env"]["VITE_API_URL"] == "/api" and bw["healing_prompt"] == ""
    # baked-at-build -> a healing prompt is produced (can't fix at deploy)
    bk = ingest(json.dumps({"services": [
        {"name": "web", "image": "org/web:1", "port": 80, "published": True,
         "connects_to": [{"service": "api", "from": "browser", "path_prefix": "/api",
                          "browser_base": {"env": "REACT_APP_API", "baked_at_build": True}}]},
        {"name": "api", "image": "org/api:1", "port": 8000}]}))
    assert "baked into the build" in bk["healing_prompt"]
    cprompt = build_prompt({"containerize": True})
    assert "containerize the app if it isn't already" in cprompt and "connects_to" in cprompt
    assert "containerize the app" not in build_prompt()

    # CHANGE 2 validation: rich env/secrets, hostname contract, empty required secret, latest tag
    v = ingest(json.dumps({"application": {"name": "app"}, "services": [
        {"name": "web", "image": "org/web:latest", "port": 80, "published": True,
         "connects_to": [{"service": "api", "hostname_in_code": "backend", "port": 8000}],
         "secrets": {"API_KEY": {"value": None, "required_to_boot": True}}},
        {"name": "api", "image": "org/api:1", "port": 8000}]}))
    errs = " | ".join(v["errors"])
    assert "must be version-pinned" in errs                          # (i) latest
    assert "not a service in this manifest" in errs and "backend" in errs   # (d) hostname mismatch
    qf = {(q["service"], q["field"]) for q in v["missing"]}
    assert ("web", "secrets.API_KEY") in qf                          # (a) empty required secret
    # rich env {value,required_to_boot} flattens for the chart
    web2 = next(s for s in v["cfg"]["services"] if s["name"] == "web")
    assert web2["required_secrets"] == ["API_KEY"]

    # replica safety (h) + db http probe (e)
    v2 = ingest(json.dumps({"services": [
        {"name": "eng", "image": "org/e:1", "port": 8000, "replicas": 3, "max_safe_replicas": 1,
         "replica_constraint_reason": "in-memory websocket engine"}]}))
    assert any("max_safe_replicas=1" in e and "websocket" in e for e in v2["errors"])

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
