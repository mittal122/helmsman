"""Same-origin reverse-proxy gateway — the universal browser↔backend fix that works on ANY
cluster, with NO ingress controller. When a frontend calls its backend from the browser, the
browser can't use cluster DNS; the answer is to put frontend+backend behind ONE origin. An
Ingress only does that if a controller is installed (local kind has none). So the bot deploys a
tiny nginx gateway that routes / -> frontend and /<prefix> -> backend, and port-forwards THE
GATEWAY. The browser hits the gateway; /api is same-origin and proxied to the backend service.

Deterministic + self-contained: a ConfigMap (nginx.conf) + Deployment + Service applied with
kubectl. No app cooperation, no external AI.
"""
import subprocess

import yaml

GATEWAY_IMAGE = "nginxinc/nginx-unprivileged:1.27-alpine"   # non-root (uid 101), listens on 8080
APPLY_TIMEOUT_S = 60


def _svc_dns(service: str, ns: str, port: int) -> str:
    return f"http://{service}.{ns}.svc.cluster.local:{port}"


def render_conf(frontend: str, fport: int, routes: list, ns: str) -> str:
    """nginx server block: each route prefix -> its backend, everything else -> the frontend.
    Most-specific prefixes first. Includes WebSocket upgrade headers."""
    ws = ["    proxy_http_version 1.1;",
          "    proxy_set_header Upgrade $http_upgrade;",
          '    proxy_set_header Connection "upgrade";',
          "    proxy_set_header Host $host;",
          "    proxy_set_header X-Forwarded-For $remote_addr;",
          "    proxy_read_timeout 3600;"]
    L = ["server {", "  listen 8080;", "  server_name _;", "  client_max_body_size 50m;"]
    for r in sorted(routes, key=lambda r: -len(str(r["path"]))):
        p = "/" + str(r["path"]).strip("/")
        up = _svc_dns(r["service"], ns, int(r["port"]))
        L += [f"  location {p}/ {{", f"    proxy_pass {up}/;", *ws, "  }"]
        L += [f"  location = {p} {{", f"    proxy_pass {up}/;", *ws, "  }"]
    L += ["  location / {", f"    proxy_pass {_svc_dns(frontend, ns, fport)};", *ws, "  }", "}"]
    return "\n".join(L)


def _manifests(name: str, ns: str, conf: str, labels: dict) -> str:
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": name + "-conf", "namespace": ns, "labels": labels},
          "data": {"default.conf": conf}}
    dep = {"apiVersion": "apps/v1", "kind": "Deployment",
           "metadata": {"name": name, "namespace": ns, "labels": labels},
           "spec": {"replicas": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
                    "template": {"metadata": {"labels": {**labels, "app.kubernetes.io/name": name}},
                                 "spec": {
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 101,
                                            "seccompProfile": {"type": "RuntimeDefault"}},
                        "containers": [{
                            "name": "gateway", "image": GATEWAY_IMAGE,
                            "ports": [{"containerPort": 8080}],
                            "resources": {"requests": {"cpu": "20m", "memory": "24Mi"},
                                          "limits": {"cpu": "200m", "memory": "128Mi"}},
                            "securityContext": {"allowPrivilegeEscalation": False,
                                                "readOnlyRootFilesystem": False,  # nginx writes /var/cache
                                                "capabilities": {"drop": ["ALL"]}},
                            "readinessProbe": {"tcpSocket": {"port": 8080}, "periodSeconds": 5},
                            "volumeMounts": [{"name": "conf", "mountPath": "/etc/nginx/conf.d"}]}],
                        "volumes": [{"name": "conf", "configMap": {"name": name + "-conf"}}]}}}}
    svc = {"apiVersion": "v1", "kind": "Service",
           "metadata": {"name": name, "namespace": ns, "labels": labels},
           "spec": {"selector": {"app.kubernetes.io/name": name},
                    "ports": [{"port": 80, "targetPort": 8080}]}}
    return "\n---\n".join(yaml.safe_dump(d) for d in (cm, dep, svc))


def deploy_gateway(stack: str, ns: str, frontend: str, fport: int, routes: list) -> str:
    """Apply the gateway (ConfigMap+Deployment+Service) and return its Service name. Idempotent —
    re-apply updates the config. The gateway is what the bot port-forwards for same-origin access."""
    name = (stack + "-gateway")[:63]
    labels = {"helmsman.dev/managed-by": "helmsman", "helmsman.dev/gateway": stack}
    manifest = _manifests(name, ns, render_conf(frontend, fport, routes, ns), labels)
    r = subprocess.run(["kubectl", "apply", "-n", ns, "-f", "-"],
                       input=manifest, capture_output=True, text=True, timeout=APPLY_TIMEOUT_S)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    # roll the config in (a ConfigMap change doesn't restart nginx on its own)
    subprocess.run(["kubectl", "rollout", "restart", "deploy", name, "-n", ns,
                    "--request-timeout=10s"], capture_output=True, text=True, timeout=20)
    subprocess.run(["kubectl", "rollout", "status", "deploy", name, "-n", ns,
                    "--timeout=90s"], capture_output=True, text=True, timeout=100)
    return name


if __name__ == "__main__":
    conf = render_conf("web", 80, [{"path": "/api", "service": "api", "port": 8000}], "default")
    assert "location /api/" in conf and "api.default.svc.cluster.local:8000" in conf
    assert "location / {" in conf and "web.default.svc.cluster.local:80" in conf
    m = _manifests("shop-gateway", "default", conf, {"x": "y"})
    docs = list(yaml.safe_load_all(m))
    assert [d["kind"] for d in docs] == ["ConfigMap", "Deployment", "Service"]
    assert docs[0]["data"]["default.conf"] == conf
    assert docs[1]["spec"]["template"]["spec"]["containers"][0]["image"] == GATEWAY_IMAGE
    print("gateway.py self-check OK")
