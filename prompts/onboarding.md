# Onboarding agent — generate a containerization prompt

## Task

The user's application is **not containerized yet** (it has no Dockerfile). You
do NOT write the Dockerfile yourself. Instead, generate a clear, ready-to-copy
prompt that the user will paste into their own AI coding assistant (Claude,
ChatGPT, Cursor, etc.) to containerize their app correctly.

## Inputs

- Application description: {{app_description}}
- Language / framework: {{language_framework}}
- Known start command (if any): {{start_command}}
- Known listening port (if any): {{port}}
- Extra notes from the user: {{notes}}

## What the generated prompt must instruct their AI to produce

- A **production-grade Dockerfile**: multi-stage build, a minimal and pinned base
  image (never `:latest`), runs as a **non-root** user, only production
  dependencies in the final image.
- A **`.dockerignore`** excluding secrets, local env files, and build junk.
- **Correct exposed port**, matching how the app actually listens.
- A basic **health endpoint** (or note if the app already has one) so Kubernetes
  can check readiness/liveness later.
- Clear final instructions to **build the image and print its full name/tag**, so
  the user can bring that image name back to Helmsman.

The generated prompt must be self-contained: the user's AI should be able to act
on it without seeing Helmsman. Write it in the second person ("Create a
Dockerfile that...").

## Output (return exactly this JSON)

```json
{
  "containerization_prompt": "string — the full prompt the user copies to their own AI assistant",
  "assumptions": ["string — anything you assumed because it wasn't provided"],
  "what_to_bring_back": "string — one line telling the user exactly what to return to Helmsman once done (the built image name/tag)"
}
```
