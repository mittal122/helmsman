import base64
import guardrails

def test_variants_include_raw_and_base64():
    v = guardrails.secret_variants({"T": "s3cret", "EMPTY": ""})
    assert "s3cret" in v
    assert base64.b64encode(b"s3cret").decode() in v
    assert "" not in v          # empty values are not redacted

def test_redact_replaces_in_nested_structures():
    variants = guardrails.secret_variants({"T": "s3cret"})
    obj = {"msg": "token is s3cret here", "list": ["x s3cret", "clean"]}
    out = guardrails.redact(obj, variants)
    assert "s3cret" not in str(out)
    assert "••••" in out["msg"]
    assert out["list"][1] == "clean"
    # original unchanged (deep copy)
    assert obj["msg"] == "token is s3cret here"

def test_redact_noop_without_variants():
    assert guardrails.redact({"a": "b"}, set()) == {"a": "b"}

def test_redact_handles_bytes_and_tuple():
    v = guardrails.secret_variants({"T": "s3cret"})
    out = guardrails.redact({"b": b"has s3cret", "t": ("s3cret", "clean")}, v)
    assert "s3cret" not in str(out)
    assert "••••" in out["b"]
    assert isinstance(out["t"], tuple) and out["t"][1] == "clean"

def test_redact_redacts_dict_keys():
    v = guardrails.secret_variants({"T": "s3cret"})
    out = guardrails.redact({"s3cret": "x"}, v)
    assert "s3cret" not in str(out)
