import agents.onboarding as onboarding
import agents.config_advisor as config_advisor
import agents.error_resolver as error_resolver

def _spy(monkeypatch, module):
    calls = {}
    def fake(prompt_file, placeholders, schema):
        calls.update(prompt_file=prompt_file, placeholders=placeholders, schema=schema)
        return {"ok": True}
    monkeypatch.setattr(module.base, "call_agent", fake)
    return calls

def test_onboarding_maps_fields(monkeypatch):
    calls = _spy(monkeypatch, onboarding)
    onboarding.generate({"app_description": "a node app", "language_framework": "node"})
    assert calls["prompt_file"] == "onboarding.md"
    assert calls["placeholders"]["app_description"] == "a node app"

def test_config_advisor_maps_fields(monkeypatch):
    calls = _spy(monkeypatch, config_advisor)
    config_advisor.advise({"name": "orders", "image": "orders:1", "port": 3000})
    assert calls["prompt_file"] == "config-advisor.md"
    assert calls["placeholders"]["app_name"] == "orders"
    assert calls["placeholders"]["detected_port"] == 3000

def test_error_resolver_maps_fields(monkeypatch):
    calls = _spy(monkeypatch, error_resolver)
    error_resolver.resolve({"failure_type": "ImagePullBackOff", "recent_logs": "boom"})
    assert calls["prompt_file"] == "error-resolution.md"
    assert calls["placeholders"]["failure_type"] == "ImagePullBackOff"
    assert calls["placeholders"]["recent_logs"] == "boom"

import agents.stack_reviewer as stack_reviewer

def test_stack_reviewer_summary_never_leaks_secret_values():
    services = [
        {"name": "backend", "image": "org/api:1", "workload": "deployment", "port": 8000,
         "published": True, "env": {"DB_HOST": "db"}, "secrets": {"DB_PASSWORD": "s3cret-value"}},
        {"name": "db", "image": "postgres:16", "workload": "deployment", "port": 5432,
         "secrets": {"POSTGRES_PASSWORD": "another-secret"}},
    ]
    s = stack_reviewer.summarize(services)
    # secret KEY names are included; secret VALUES never are (redaction invariant)
    assert "DB_PASSWORD" in s and "POSTGRES_PASSWORD" in s
    assert "s3cret-value" not in s and "another-secret" not in s
    assert "name=backend" in s and "image=postgres:16" in s

def test_stack_reviewer_review_maps_fields(monkeypatch):
    calls = _spy(monkeypatch, stack_reviewer)
    stack_reviewer.review([{"name": "web", "image": "w:1", "port": 80, "secrets": {"K": "v"}}])
    assert calls["prompt_file"] == "stack-review.md"
    assert "name=web" in calls["placeholders"]["stack"] and "v" not in calls["placeholders"]["stack"]

def test_call_agent_routes_to_nim_when_configured(monkeypatch):
    import json as _json
    import urllib.request
    import agents.base as base
    monkeypatch.setenv("NIM_API_KEY", "test-key")
    monkeypatch.setenv("NIM_MODEL", "meta/llama-3.1-70b-instruct")
    cap = {}
    class _Resp:
        def __init__(self, p): self._p = p
        def read(self): return self._p
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def _fake_urlopen(req, timeout=60):
        cap["url"] = req.full_url
        cap["auth"] = req.headers.get("Authorization")
        cap["model"] = _json.loads(req.data)["model"]
        # a model that wraps JSON in ```json fences -> _extract_json must still parse it
        return _Resp(_json.dumps({"choices": [{"message": {"content": '```json\n{"root_cause":"x"}\n```'}}]}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    out = base.call_agent("error-resolution.md", {"failure_type": "X"}, {"type": "object"})
    assert out == {"root_cause": "x"}
    assert cap["url"].endswith("/chat/completions")
    assert cap["auth"] == "Bearer test-key"
    assert cap["model"] == "meta/llama-3.1-70b-instruct"

def test_no_nim_key_means_anthropic_path(monkeypatch):
    import agents.base as base
    monkeypatch.delenv("NIM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert base._openai_compatible() is None
