import os
import re
import tempfile
from cryptography.fernet import Fernet

DATA_DIR = os.environ.get("KUBECONFIG_DATA_DIR",
                          os.path.join(os.path.dirname(__file__), "data", "kubeconfigs"))
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\Z")

def _fernet() -> Fernet:
    key = os.environ.get("KUBECONFIG_ENC_KEY")
    if not key:
        raise RuntimeError("KUBECONFIG_ENC_KEY not set — refusing to store kubeconfig unencrypted")
    return Fernet(key.encode())

def _path(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise ValueError("invalid kubeconfig name (RFC1123)")
    return os.path.join(DATA_DIR, f"{name}.kubeconfig.enc")

def save(name: str, raw: bytes) -> None:
    path = _path(name)
    os.makedirs(DATA_DIR, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)  # makedirs mode is umask-masked and skipped if dir preexists
    token = _fernet().encrypt(raw)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(token)
    os.chmod(path, 0o600)  # re-assert on overwrite of a preexisting looser-permissioned file

def list_names() -> list[str]:
    if not os.path.isdir(DATA_DIR):
        return []
    return [f[:-len(".kubeconfig.enc")] for f in os.listdir(DATA_DIR)
            if f.endswith(".kubeconfig.enc")]

def delete(name: str) -> bool:
    path = _path(name)
    if os.path.exists(path):
        os.unlink(path)
        return True
    return False

def decrypt_to_tempfile(name: str) -> str:
    path = _path(name)
    if not os.path.exists(path):
        raise KeyError(name)
    raw = _fernet().decrypt(open(path, "rb").read())
    fd, tmp = tempfile.mkstemp(suffix=".kubeconfig")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return tmp
