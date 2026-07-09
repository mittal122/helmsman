import json
import os
from pathlib import Path

import anthropic

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
MODEL = "claude-opus-4-8"
MAX_TOKENS = 2048

_client = None

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

def call_agent(prompt_file: str, placeholders: dict, schema: dict) -> dict:
    system = _load("_system.md")
    user = _fill(_load(prompt_file), placeholders)
    resp = _client_().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)
