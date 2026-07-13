"""Parse a docker-compose file into a list of normalized deploy cfgs — one per service.

The platform deploys ONE image → ONE Helm release. A compose stack is just N of those
sharing a namespace: each service becomes a standard cfg (same shape coordinator.run
consumes), rendered by the SAME chart (locked decision #4 — never generate manifests /
never use Kompose). Service-to-service networking is free: each K8s Service is named after
its compose service, so `db:5432` resolves via cluster DNS exactly as in compose.

Pure + deterministic + testable — touches no cluster, runs no repo code.

v1 scope (see spec 2026-07-12-docker-compose-multi-service-deploy-design.md):
- image-only services (a `build:`-only service is a hard error — pre-build it first)
- ports, environment/secrets (auto-classified), command/entrypoint, replicas, resources,
  healthcheck→probe, named volumes→PVC, user, depends_on ordering
- warn-and-skip: networks, restart, bind/anonymous mounts, env_file, profiles/configs/extends
"""
import re
import shlex

import yaml

# env keys that look like a credential -> routed to K8s Secret (redacted), not ConfigMap.
# Over-classifying is the safe direction (a non-secret merely gets hidden).
_SECRETISH = re.compile(r"(?i)(PASSWORD|PASSWD|SECRET|TOKEN|CREDENTIAL|APIKEY|KEY)")

# RFC1123 label — the compose service name becomes a K8s Service/Deployment name (DNS).
_RFC1123 = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")

def _k8s_name(s: str) -> str:
    """Compose volume names allow '_' (e.g. postgres_data); K8s names don't. Coerce to a
    valid RFC1123 label so the PVC/volume name is accepted."""
    s = re.sub(r"[^a-z0-9-]", "-", (s or "").lower()).strip("-")
    return s or "vol"


# ${VAR} / ${VAR:-default} / ${VAR:?err} / $VAR — compose interpolates BEFORE parsing.
_VAR = re.compile(r"\$(?:\$|\{([A-Za-z_][A-Za-z0-9_]*)(?::?[-?+])?([^}]*)\}|([A-Za-z_][A-Za-z0-9_]*))")

def interpolate(text: str, env: dict | None = None) -> tuple[str, list]:
    """Resolve docker-compose variable substitution against `env` (NOT the server's os.environ
    — that would leak host env into a user's deploy). Returns (text, warnings). `$$` -> `$`.
    A required var (`${VAR:?...}`) with no value substitutes empty and warns, so a value the
    user forgot surfaces clearly instead of a cryptic runtime crash."""
    env = env or {}
    warns: list = []

    def repl(m):
        if m.group(0) == "$$":
            return "$"
        name = m.group(1) or m.group(3)
        rest = m.group(2) or ""
        # figure out the operator (:- , - , :? , ? , :+ , +) from the matched text
        raw = m.group(0)
        op = ""
        if name and "{" in raw:
            after = raw[raw.index(name) + len(name):-1]  # between name and closing }
            if after[:2] in (":-", ":?", ":+"):
                op = after[:2]
            elif after[:1] in ("-", "?", "+"):
                op = after[:1]
        val = env.get(name)
        has = val not in (None, "") if op.startswith(":") else val is not None
        if op in (":-", "-"):
            return val if has else rest
        if op in (":+", "+"):
            return rest if has else ""
        if op in (":?", "?"):
            if has:
                return val
            warns.append(f"variable ${{{name}}} is required but no value was provided — "
                         f"substituted empty (set it in the env fields)")
            return ""
        if val is None:
            warns.append(f"variable ${{{name}}} is unset — substituted empty")
            return ""
        return val

    return _VAR.sub(repl, text), warns

def _container_port(spec) -> int | None:
    """Extract the container (target) port from a compose ports entry."""
    if isinstance(spec, dict):                       # long form {target, published}
        t = spec.get("target")
        return int(t) if t is not None else None
    s = str(spec).split("/")[0]                       # drop /tcp|/udp
    parts = s.split(":")
    tail = parts[-1]                                  # "host:container" or "ip:host:container" or "container"
    if "-" in tail:                                   # port range "3000-3005" -> first
        tail = tail.split("-")[0]
    try:
        return int(tail)
    except ValueError:
        return None


def _norm_env(env) -> dict:
    """compose environment: dict {K:V} or list ['K=V','K'] -> {K: str(V)}."""
    out = {}
    if isinstance(env, dict):
        for k, v in env.items():
            out[str(k)] = "" if v is None else str(v)
    elif isinstance(env, list):
        for item in env:
            k, _, v = str(item).partition("=")
            out[k] = v
    return out


def _split_env_secrets(env: dict) -> tuple[dict, dict]:
    plain, secret = {}, {}
    for k, v in env.items():
        (secret if _SECRETISH.search(k) else plain)[k] = v
    return plain, secret


def _as_argv(v) -> list:
    """compose command/entrypoint: list -> as-is; string -> shell-split into argv."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return shlex.split(str(v))


def _cpu(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if s.endswith("m"):
        return s
    try:
        return f"{int(round(float(s) * 1000))}m"      # "0.5" cpus -> "500m"
    except ValueError:
        return None


_MEM_UNIT = {"b": "", "k": "Ki", "m": "Mi", "g": "Gi", "t": "Ti"}

def _mem(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([bkmgt])?b?$", s, re.I)
    if not m:
        return None
    num, unit = m.group(1), (m.group(2) or "").lower()
    num = str(int(float(num)))
    return num + _MEM_UNIT.get(unit, "Mi")            # bare number -> Mi (compose uses bytes, but Mi is the sane k8s default)


def _resources(deploy: dict) -> dict:
    res = (deploy or {}).get("resources") or {}
    limits, reserv = res.get("limits") or {}, res.get("reservations") or {}
    out = {}
    req = {"cpu": _cpu(reserv.get("cpus")), "memory": _mem(reserv.get("memory"))}
    lim = {"cpu": _cpu(limits.get("cpus")), "memory": _mem(limits.get("memory"))}
    req = {k: v for k, v in req.items() if v}
    lim = {k: v for k, v in lim.items() if v}
    if req:
        out["requests"] = req
    if lim:
        out["limits"] = lim
    return out


def _probe(svc: dict, port: int | None, warns: list, name: str) -> dict:
    """healthcheck -> exec probe; none given -> TCP probe on the port (works for db/redis/
    anything). NEVER an HTTP probe from compose (compose has no HTTP path) — an HTTP
    liveness probe on a database is the #1 way a multi-service deploy crash-loops."""
    hc = svc.get("healthcheck")
    if isinstance(hc, dict):
        if hc.get("disable"):
            return {"type": "none"}
        test = hc.get("test")
        if isinstance(test, list) and test:
            kind = test[0]
            if kind == "NONE":
                return {"type": "none"}
            if kind == "CMD":
                return {"type": "exec", "command": [str(x) for x in test[1:]]}
            if kind == "CMD-SHELL":
                return {"type": "exec", "command": ["sh", "-c", " ".join(str(x) for x in test[1:])]}
            return {"type": "exec", "command": [str(x) for x in test]}
        if isinstance(test, str) and test:
            return {"type": "exec", "command": ["sh", "-c", test]}
    if port:
        return {"type": "tcp"}
    warns.append(f"{name}: no healthcheck and no port — no liveness/readiness probe set")
    return {"type": "none"}


def _volumes(svc: dict, warns: list, name: str) -> list:
    """Named volumes -> PVC entries. Bind/anonymous mounts warn-and-skip (hostPath is wrong
    on multi-node k8s; anonymous has no stable identity for a PVC)."""
    out = []
    for v in svc.get("volumes") or []:
        if isinstance(v, dict):
            src, tgt = v.get("source"), v.get("target")
        else:
            parts = str(v).split(":")
            if len(parts) >= 2:
                src, tgt = parts[0], parts[1]
            else:
                src, tgt = None, parts[0]
        if not tgt:
            continue
        if not src or src.startswith(".") or src.startswith("/"):
            warns.append(f"{name}: volume '{v}' skipped (bind/anonymous mounts aren't supported; "
                         f"use a named volume for persistence)")
            continue
        out.append({"name": _k8s_name(src), "mountPath": tgt, "size": "1Gi"})
    return out


def _build_spec(b) -> dict:
    """compose build: -> a normalized build spec. String form is the context dir; long form is
    {context, dockerfile}. git_repo is empty = inherit the stack's repo (a compose monorepo)."""
    if isinstance(b, dict):
        subdir = str(b.get("context") or "").strip()
        dockerfile = str(b.get("dockerfile") or "").strip()
    else:
        subdir, dockerfile = str(b or "").strip(), ""
    subdir = subdir.lstrip(".").lstrip("/").rstrip("/")   # "./api/" -> "api"
    if ".." in subdir or ".." in dockerfile or dockerfile.startswith("/"):
        raise ValueError(f"invalid build path '{b}'")
    return {"git_repo": "", "git_branch": "", "git_ref": "", "subdir": subdir, "dockerfile": dockerfile}


def _topo_order(names: list, deps: dict) -> list:
    """Return service names in depends_on order (dependencies first). Cycle -> ValueError."""
    order, temp, perm = [], set(), set()

    def visit(n):
        if n in perm:
            return
        if n in temp:
            raise ValueError(f"depends_on cycle involving '{n}'")
        temp.add(n)
        for d in deps.get(n, []):
            if d in names:
                visit(d)
        temp.discard(n)
        perm.add(n)
        order.append(n)

    for n in names:
        visit(n)
    return order


def parse(yaml_text: str, env: dict | None = None) -> tuple[list, list]:
    """docker-compose YAML -> (services, warnings). services is a list of normalized deploy
    cfgs in depends_on order. `env` supplies values for ${VAR} interpolation. Raises ValueError
    on malformed input / build-only service / cycle so the caller can 422 with a clear message."""
    yaml_text, warns = interpolate(yaml_text, env)     # resolve ${VAR} BEFORE parsing (compose order)
    try:
        doc = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML: {str(e).splitlines()[0] if str(e) else e}")
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict) or not doc["services"]:
        raise ValueError("no 'services:' found in the compose file")

    if doc.get("networks"):
        warns.append("custom networks ignored — all services share one namespace (compose's default flat network)")
    if doc.get("configs") or doc.get("secrets"):
        warns.append("top-level configs/secrets not supported in v1 — ignored")

    svcmap = doc["services"]
    deps = {}
    for name, svc in svcmap.items():
        svc = svc or {}
        d = svc.get("depends_on")
        if isinstance(d, dict):
            deps[name] = list(d.keys())
        elif isinstance(d, list):
            deps[name] = list(d)
        else:
            deps[name] = []

    services = []
    for name in _topo_order(list(svcmap.keys()), deps):
        svc = svcmap[name] or {}
        if not _RFC1123.match(name):
            raise ValueError(f"service name '{name}' is not a valid Kubernetes name (RFC1123: lowercase alnum + '-')")
        # a service ships EITHER a pre-built image OR a build: spec (built from source at
        # deploy time). The build context is a subdir of the SAME repo the stack is deployed
        # from (git_repo inherited by the coordinator's Build stage).
        build = None
        img = str(svc.get("image") or "")
        if not img:
            if svc.get("build") is not None:
                build = _build_spec(svc["build"])
            else:
                raise ValueError(f"service '{name}' has no image (and no build: to build one)")
        elif img.startswith("-") or any(c.isspace() for c in img):
            raise ValueError(f"service '{name}' has an invalid image reference")

        ports = [p for p in (_container_port(x) for x in (svc.get("ports") or [])) if p]
        # expose: internal-only ports (not browser-facing) — still needed on the Service so
        # sibling services can reach e.g. db:5432 by DNS.
        exposed = [p for p in (_container_port(x) for x in (svc.get("expose") or [])) if p]
        all_ports = ports + [p for p in exposed if p not in ports]
        port = all_ports[0] if all_ports else 0
        extra = all_ports[1:]

        env = _norm_env(svc.get("environment"))
        if svc.get("env_file"):
            warns.append(f"{name}: env_file ignored (v1 reads only inline environment)")
        plain, secret = _split_env_secrets(env)

        if svc.get("restart"):
            warns.append(f"{name}: restart policy '{svc['restart']}' ignored (Deployments always restart)")
        if svc.get("profiles"):
            warns.append(f"{name}: profiles ignored")

        replicas = 1
        deploy = svc.get("deploy") or {}
        if isinstance(deploy, dict) and deploy.get("replicas") is not None:
            replicas = int(deploy["replicas"])

        user = svc.get("user")
        run_as_user = None
        if user is not None:
            try:
                run_as_user = int(str(user).split(":")[0])
            except ValueError:
                warns.append(f"{name}: non-numeric user '{user}' ignored")

        cfg = {
            "name": name,
            "image": img,
            "port": port or 8080,
            "replicas": replicas,
            "env": plain,
            "secrets": secret,
            "command": _as_argv(svc.get("entrypoint")),   # compose entrypoint -> container command
            "args": _as_argv(svc.get("command")),          # compose command    -> container args
            "extra_ports": extra,
            "resources": _resources(deploy),
            "probe": _probe(svc, port, warns, name),
            "volumes": _volumes(svc, warns, name),
            "run_as_user": run_as_user,
            "published": bool(ports),          # had a ports: mapping -> browser-facing -> port-forward it
            "build": build,                    # None = pre-built image; else a from-source spec
        }
        services.append(cfg)

    return services, warns


if __name__ == "__main__":
    stack = """
services:
  web:
    image: nginx:1.27
    ports: ["8080:80"]
    depends_on: [db]
    environment:
      APP_ENV: prod
      DB_PASSWORD: s3cret
    deploy:
      replicas: 2
      resources:
        limits: {cpus: "0.5", memory: 256M}
        reservations: {cpus: "0.1", memory: 64M}
  db:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: pw
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./local:/bind        # bind -> skipped
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "postgres"]
    user: "999"
    restart: always
networks:
  default: {}
"""
    svcs, w = parse(stack)
    names = [s["name"] for s in svcs]
    assert names == ["db", "web"], names                       # depends_on: db before web
    web = next(s for s in svcs if s["name"] == "web")
    db = next(s for s in svcs if s["name"] == "db")
    assert web["port"] == 80 and web["replicas"] == 2, web
    assert web["env"] == {"APP_ENV": "prod"}, web["env"]        # password split out
    assert web["secrets"] == {"DB_PASSWORD": "s3cret"}, web["secrets"]
    assert web["resources"]["limits"] == {"cpu": "500m", "memory": "256Mi"}, web["resources"]
    assert web["resources"]["requests"] == {"cpu": "100m", "memory": "64Mi"}, web["resources"]
    assert web["probe"] == {"type": "tcp"}, web["probe"]        # no healthcheck, has port -> TCP
    assert db["probe"]["type"] == "exec" and db["probe"]["command"][0] == "pg_isready", db["probe"]
    assert db["volumes"] == [{"name": "pgdata", "mountPath": "/var/lib/postgresql/data", "size": "1Gi"}], db["volumes"]
    assert db["run_as_user"] == 999, db
    assert db["secrets"] == {"POSTGRES_PASSWORD": "pw"}, db["secrets"]
    assert any("networks" in x for x in w) and any("bind/anonymous" in x for x in w), w
    assert any("restart" in x for x in w), w
    # a build: service now produces a build spec (built from source at deploy time)
    bsvcs, _ = parse("services:\n  api:\n    build: ./api\n    ports: ['8000:8000']\n")
    assert bsvcs[0]["build"] == {"git_repo": "", "git_branch": "", "git_ref": "", "subdir": "api", "dockerfile": ""}, bsvcs[0]["build"]
    assert bsvcs[0]["image"] == "", bsvcs[0]
    # long-form build with an explicit dockerfile
    b2, _ = parse("services:\n  api:\n    build:\n      context: .\n      dockerfile: api/Dockerfile\n    ports: ['80']\n")
    assert b2[0]["build"]["dockerfile"] == "api/Dockerfile" and b2[0]["build"]["subdir"] == "", b2[0]["build"]
    # a pre-built image still has build=None
    p, _ = parse("services:\n  web:\n    image: nginx:1.27\n")
    assert p[0]["build"] is None, p[0]
    # cycle
    try:
        parse("services:\n  a:\n    image: i\n    depends_on: [b]\n  b:\n    image: i\n    depends_on: [a]\n")
        assert False, "cycle should raise"
    except ValueError:
        pass
    print("compose.py self-check OK")
