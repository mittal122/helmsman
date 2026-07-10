import os
import pytest
from tools import builder

def test_valid_url_accepts_https_and_ssh():
    assert builder.valid_url("https://github.com/org/app.git")
    assert builder.valid_url("git@github.com:org/app.git")

def test_valid_url_rejects_junk_and_injection():
    assert not builder.valid_url("")
    assert not builder.valid_url("ftp://x/y")
    assert not builder.valid_url("https://x/y; rm -rf /")   # space + ; blocked
    assert not builder.valid_url("https://x/$(whoami)")

def test_display_url_strips_credentials():
    assert builder.display_url("https://tok3n@github.com/o/r.git") == "https://github.com/o/r.git"
    assert builder.display_url("https://user:pass@host/o/r.git") == "https://host/o/r.git"
    assert builder.display_url("https://github.com/o/r.git") == "https://github.com/o/r.git"

def test_image_tag_local_and_registry(monkeypatch):
    monkeypatch.delenv("REGISTRY", raising=False)
    assert builder.image_tag("app", "abc123") == "app:src-abc123"
    monkeypatch.setenv("REGISTRY", "reg.io/team/")
    assert builder.image_tag("app", "abc123") == "reg.io/team/app:src-abc123"

def test_make_available_kind_loads_into_named_cluster(monkeypatch):
    seen = {}
    monkeypatch.setattr(builder, "_run", lambda args, **k: seen.setdefault("args", args))
    assert builder.make_available("app:src-x", "kind-helmsman") == "kind"
    assert seen["args"] == ["kind", "load", "docker-image", "app:src-x", "--name", "helmsman"]

def test_make_available_minikube(monkeypatch):
    seen = {}
    monkeypatch.setattr(builder, "_run", lambda args, **k: seen.setdefault("args", args))
    assert builder.make_available("app:src-x", "minikube") == "minikube"
    assert seen["args"] == ["minikube", "image", "load", "app:src-x"]

def test_make_available_remote_without_registry_raises(monkeypatch):
    monkeypatch.delenv("REGISTRY", raising=False)
    monkeypatch.setattr(builder, "_run", lambda *a, **k: "")
    with pytest.raises(RuntimeError):
        builder.make_available("app:src-x", "gke_prod")

def test_make_available_remote_with_registry_pushes(monkeypatch):
    monkeypatch.setenv("REGISTRY", "reg.io")
    seen = {}
    monkeypatch.setattr(builder, "_run", lambda args, **k: seen.setdefault("args", args))
    assert builder.make_available("reg.io/app:src-x", "gke_prod") == "registry"
    assert seen["args"] == ["docker", "push", "reg.io/app:src-x"]

def test_clone_rejects_bad_url_before_network():
    with pytest.raises(ValueError):
        builder.clone("not a url")

def test_build_missing_dockerfile_raises(tmp_path):
    with pytest.raises(ValueError):
        builder.build(str(tmp_path), "app:1", "Dockerfile")

def test_build_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        builder.build(str(tmp_path), "app:1", "../etc/Dockerfile")
