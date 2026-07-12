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

def test_valid_ref_blocks_flag_injection():
    assert builder.valid_ref("main") and builder.valid_ref("feature/x") and builder.valid_ref("v1.2.3")
    assert builder.valid_ref("a1b2c3d")
    assert not builder.valid_ref("-x")                     # leading dash = flag
    assert not builder.valid_ref("--upload-pack=/bin/sh")  # argv flag smuggling
    assert not builder.valid_ref("a b") and not builder.valid_ref("")

def test_clone_rejects_flaglike_branch_and_ref_before_network():
    with pytest.raises(ValueError):
        builder.clone("https://github.com/o/r.git", branch="--upload-pack=x")
    with pytest.raises(ValueError):
        builder.clone("https://github.com/o/r.git", ref="-x")

def test_clone_rejects_bad_url_before_network():
    with pytest.raises(ValueError):
        builder.clone("not a url")

def test_build_missing_dockerfile_raises(tmp_path):
    with pytest.raises(ValueError):
        builder.build(str(tmp_path), "app:1", "Dockerfile")

def test_build_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        builder.build(str(tmp_path), "app:1", "../etc/Dockerfile")

def _touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("FROM scratch\n")

def test_list_dockerfiles_detects_and_sorts(tmp_path):
    _touch(tmp_path / "Dockerfile")
    _touch(tmp_path / "Dockerfile.prod")
    _touch(tmp_path / "api" / "Dockerfile")
    _touch(tmp_path / "docker" / "web.Dockerfile")
    _touch(tmp_path / ".git" / "Dockerfile")            # ignored dir
    _touch(tmp_path / "node_modules" / "x" / "Dockerfile")  # ignored dir
    _touch(tmp_path / "README.md")                       # not a dockerfile
    got = builder.list_dockerfiles(str(tmp_path))
    assert got == ["Dockerfile", "Dockerfile.prod", "api/Dockerfile", "docker/web.Dockerfile"]

def test_list_dockerfiles_respects_depth_cap(tmp_path):
    _touch(tmp_path / "a" / "b" / "c" / "d" / "e" / "Dockerfile")   # depth 5 > cap 4
    assert builder.list_dockerfiles(str(tmp_path), max_depth=4) == []

def test_list_dockerfiles_caps_count(tmp_path):
    for i in range(60):
        _touch(tmp_path / f"svc{i}" / "Dockerfile")
    assert len(builder.list_dockerfiles(str(tmp_path), max_files=50)) == 50
