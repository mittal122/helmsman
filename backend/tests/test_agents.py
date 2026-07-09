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
