import os
import subprocess
import tempfile
import yaml

CHART_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "chart"))

def build_values(cfg: dict) -> dict:
    return {
        "name": cfg["name"],
        "image": cfg["image"],
        "port": int(cfg.get("port", 8080)),
        "replicas": int(cfg.get("replicas", 2)),
    }

def render(cfg: dict) -> str:
    values = build_values(cfg)
    ns = cfg.get("namespace", "default")
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
        vfile = f.name
    try:
        out = subprocess.run(
            ["helm", "template", values["name"], CHART_DIR, "-f", vfile, "--namespace", ns],
            capture_output=True, text=True, check=True,
        )
        return out.stdout
    finally:
        os.unlink(vfile)
