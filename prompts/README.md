# Prompts — the AI control surface

Every LLM agent in Helmsman reads its instructions from a file in this folder.
Change how an agent thinks by editing its prompt here — **no code changes, no
rebuild.** This is the intended control surface for the whole AI flow.

## Files

| File | Used by | Job |
|---|---|---|
| `_system.md` | **every** LLM call | Shared preamble: role + the non-negotiable safety rules. Prepended to all agent prompts. |
| `onboarding.md` | Onboarding agent | Generate a containerization prompt the user gives to their own AI assistant. |
| `config-advisor.md` | Config-advisor agent | Suggest + explain deployment settings for the user to confirm. |
| `error-resolution.md` | Error-resolution agent | Explain a failure's root cause and recommend a fix. |

There are only three LLM agents by design (spec §2.1). Everything else in the
platform is deterministic and has no prompt.

## How the code uses these (Phase 3)

Each agent in `backend/agents/` loads `_system.md` + its own file, fills the
`{{placeholders}}` with runtime values, and calls Claude asking for the
structured JSON output declared at the bottom of each file.

```
final_prompt = read("_system.md") + read("config-advisor.md")
final_prompt = fill(final_prompt, app_name="...", image="...", ...)
claude(final_prompt, output_schema=SCHEMA)   # structured output, validated
```

## Rules for editing

- **Placeholders** use `{{double_braces}}`. Don't rename them without updating
  the agent code that fills them.
- **Keep the safety rules in `_system.md`.** They are design invariants,
  not style — removing them breaks the platform's guarantees.
- **Keep the output schema at the bottom of each file.** The code validates
  against it; drifting from it will fail the call.
- Plain language always — the end user may not know Kubernetes.
