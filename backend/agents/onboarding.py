from agents import base

SCHEMA = {
    "type": "object",
    "properties": {
        "containerization_prompt": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "what_to_bring_back": {"type": "string"},
    },
    "required": ["containerization_prompt", "assumptions", "what_to_bring_back"],
    "additionalProperties": False,
}

def generate(cfg: dict) -> dict:
    return base.call_agent("onboarding.md", {
        "app_description": cfg.get("app_description", ""),
        "language_framework": cfg.get("language_framework", ""),
        "start_command": cfg.get("start_command", ""),
        "port": cfg.get("port", ""),
        "notes": cfg.get("notes", ""),
    }, SCHEMA)
