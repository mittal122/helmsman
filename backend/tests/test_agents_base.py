import agents.base as base

class _Text:
    type = "text"
    def __init__(self, t): self.text = t

class _Resp:
    def __init__(self, t): self.content = [_Text(t)]

def _fake_client(captured):
    class _Msgs:
        def create(self, **kw):
            captured.update(kw)
            return _Resp('{"ok": true, "value": 42}')
    class _C:
        messages = _Msgs()
    return _C()

def test_call_agent_fills_placeholders_and_returns_parsed(monkeypatch):
    captured = {}
    monkeypatch.setattr(base.anthropic, "Anthropic", lambda: _fake_client(captured))
    base._client = None  # reset the lazy singleton
    out = base.call_agent("config-advisor.md",
                          {"app_name": "orders", "image": "orders:1"},
                          {"type": "object"})
    assert out == {"ok": True, "value": 42}
    # system is the shared preamble
    assert "Helmsman" in captured["system"]
    # placeholder filled into the user message
    user = captured["messages"][0]["content"]
    assert "orders" in user and "{{app_name}}" not in user
    # structured output requested
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert captured["model"] == "claude-opus-4-8"

def test_fill_leaves_unknown_placeholders_untouched():
    assert base._fill("a {{x}} b", {"x": "Z"}) == "a Z b"
