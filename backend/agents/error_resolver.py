from agents import base

SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string"},
        "plain_explanation": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "recommended_action": {"type": "string"},
        "fix_prompt": {"type": "string"},
        "auto_remediable": {"type": "boolean"},
        "suggested_auto_action": {"type": "string"},
        "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "suspicious_input_detected": {"type": "boolean"},
    },
    "required": ["root_cause", "plain_explanation", "evidence", "recommended_action",
                 "fix_prompt", "auto_remediable", "suggested_auto_action",
                 "severity", "suspicious_input_detected"],
    "additionalProperties": False,
}

def resolve(ctx: dict) -> dict:
    return base.call_agent("error-resolution.md", {
        "failure_type": ctx.get("failure_type", ""),
        "pod_status": ctx.get("pod_status", ""),
        "recent_events": ctx.get("recent_events", ""),
        "recent_logs": ctx.get("recent_logs", ""),
        "config_summary": ctx.get("config_summary", ""),
    }, SCHEMA)
