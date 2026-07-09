import os
import pytest
from cryptography.fernet import Fernet
import kubeconfig_store as ks

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBECONFIG_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(ks, "DATA_DIR", str(tmp_path))
    return ks

def test_save_encrypts_at_rest(store, tmp_path):
    store.save("prod", b"apiVersion: v1\nkind: Config\n")
    blob = (tmp_path / "prod.kubeconfig.enc").read_bytes()
    assert b"apiVersion" not in blob           # ciphertext, not plaintext
    assert oct((tmp_path / "prod.kubeconfig.enc").stat().st_mode)[-3:] == "600"

def test_roundtrip_via_tempfile(store):
    store.save("prod", b"HELLO-KUBECONFIG")
    path = store.decrypt_to_tempfile("prod")
    try:
        assert open(path, "rb").read() == b"HELLO-KUBECONFIG"
        assert oct(os.stat(path).st_mode)[-3:] == "600"
    finally:
        os.unlink(path)

def test_list_and_delete(store):
    store.save("a", b"x"); store.save("b", b"y")
    assert sorted(store.list_names()) == ["a", "b"]
    assert store.delete("a") is True
    assert store.list_names() == ["b"]

def test_requires_key(monkeypatch, tmp_path):
    monkeypatch.delenv("KUBECONFIG_ENC_KEY", raising=False)
    monkeypatch.setattr(ks, "DATA_DIR", str(tmp_path))
    with pytest.raises(RuntimeError):
        ks.save("x", b"y")

def test_rejects_bad_name(store):
    with pytest.raises(ValueError):
        store.save("../evil", b"y")

def test_rejects_trailing_newline_name(store):
    # regex $ (not \Z) would match before a trailing \n — must be rejected
    with pytest.raises(ValueError):
        store.save("prod\n", b"x")

def test_data_dir_mode_0700(store, tmp_path):
    os.chmod(str(tmp_path), 0o777)  # loosen first so save() must re-tighten it
    store.save("prod", b"x")
    assert oct(tmp_path.stat().st_mode)[-3:] == "700"
