from agents import base

SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "value": {"type": "string"},
                    "reason": {"type": "string"},
                    "guessed": {"type": "boolean"},
                },
                "required": ["field", "value", "reason", "guessed"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["suggestions", "summary"],
    "additionalProperties": False,
}

def advise(cfg: dict) -> dict:
    return base.call_agent("config-advisor.md", {
        "app_name": cfg.get("name", ""),
        "image": cfg.get("image", ""),
        "detected_port": cfg.get("port", ""),
        "language_framework": cfg.get("language_framework", ""),
        "expected_traffic": cfg.get("expected_traffic", ""),
        "notes": cfg.get("notes", ""),
    }, SCHEMA)
