"""AI-brain advisory stack reviewer — flags multi-service wiring problems the deterministic rules
can't see (an app depending on a service that isn't in the stack, etc.). Advisory ONLY: findings
are surfaced as suggestions; they never gate a cluster mutation. Fed the stack STRUCTURE only —
names, images, types, ports, and env/secret KEY names — NEVER secret values (redaction invariant).
"""
from agents import base

SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "issue": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["service", "issue", "suggestion", "severity"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["findings"],
    "additionalProperties": False,
}


def summarize(services: list) -> str:
    """Redaction-safe one-line-per-service summary: NO env/secret VALUES, only key names."""
    lines = []
    for s in services:
        parts = [f"name={s.get('name')}", f"image={s.get('image') or '(build)'}",
                 f"type={s.get('workload', 'deployment')}"]
        if s.get("port"):
            parts.append(f"port={s.get('port')}")
        if s.get("published"):
            parts.append("published")
        if s.get("env"):
            parts.append("env_keys=[" + ",".join(sorted((s.get("env") or {}).keys())) + "]")
        if s.get("secrets"):
            parts.append("secret_keys=[" + ",".join(sorted((s.get("secrets") or {}).keys())) + "]")
        if s.get("ingress_host"):
            parts.append("ingress")
        lines.append("- " + "  ".join(parts))
    return "\n".join(lines)


def review(services: list) -> dict:
    return base.call_agent("stack-review.md", {"stack": summarize(services)}, SCHEMA)
