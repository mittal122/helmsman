import json
import os
from pathlib import Path

import anthropic

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
MODEL = "claude-opus-4-8"
MAX_TOKENS = 2048

# Provider is pluggable. Default = Anthropic (locked decision #3). Set NIM_API_KEY (or a generic
# OPENAI_API_KEY) to route the advisory LLM agents to an OpenAI-COMPATIBLE endpoint instead —
# e.g. NVIDIA NIM (https://integrate.api.nvidia.com/v1). The LLM stays advisory-only either way;
# nothing it returns is applied without deterministic validation.
NIM_DEFAULT_BASE = "https://integrate.api.nvidia.com/v1"
# default to a small, fast, always-warm model so it works out of the box; larger models
# (e.g. meta/llama-3.3-70b-instruct) give better quality but can cold-start slowly on the
# serverless tier — set NIM_MODEL + a higher NIM_TIMEOUT for those.
NIM_DEFAULT_MODEL = "meta/llama-3.1-8b-instruct"

_client = None

def _openai_compatible():
    """Return (base_url, api_key, model) if an OpenAI-compatible provider (NIM) is configured,
    else None. NIM_* takes precedence; OPENAI_* is also accepted."""
    key = os.environ.get("NIM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    base = (os.environ.get("NIM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
            or NIM_DEFAULT_BASE).rstrip("/")
    model = os.environ.get("NIM_MODEL") or os.environ.get("OPENAI_MODEL") or NIM_DEFAULT_MODEL
    return base, key, model

def _client_():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client

def _load(name: str) -> str:
    return (PROMPTS_DIR / name).read_text()

def _fill(text: str, values: dict) -> str:
    for k, v in (values or {}).items():
        text = text.replace("{{" + k + "}}", "" if v is None else str(v))
    return text

def _extract_json(s: str) -> str:
    # some OpenAI-compatible models wrap JSON in prose or ```json fences — take the outer object.
    a, b = s.find("{"), s.rfind("}")
    return s[a:b + 1] if (a >= 0 and b > a) else s

def _call_openai_compatible(system: str, user: str, base: str, key: str, model: str) -> dict:
    import urllib.request
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system +
             "\n\nReturn ONLY a single JSON object matching the requested shape — no prose, no markdown fences."},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body, method="POST",
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    timeout = int(os.environ.get("NIM_TIMEOUT", "120"))   # cold-starting big models are slow
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    content = data["choices"][0]["message"]["content"]
    return json.loads(_extract_json(content))

def call_agent(prompt_file: str, placeholders: dict, schema: dict) -> dict:
    system = _load("_system.md")
    user = _fill(_load(prompt_file), placeholders)
    nim = _openai_compatible()
    if nim:
        base, key, model = nim
        return _call_openai_compatible(system, user, base, key, model)
    resp = _client_().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise ValueError("Claude response contained no text block")
    return json.loads(text)
