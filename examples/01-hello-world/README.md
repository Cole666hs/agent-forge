# Example 1 — Hello World

The smallest possible AgentForge program: a workflow that receives a message,
echoes it back, persists state to SQLite, and exits. ~40 lines.

## What you'll learn

- Loading a workflow from a YAML file
- Running it against an LLM
- Inspecting the state and runs

## Run it

```bash
# From the repo root, with the venv active
.venv/bin/python examples/01-hello-world/run.py
```

Expected output (something like):

```
run_id: run_xxxxxxxx
status: success
steps: 1
echo: hello from agentforge
```

## Files

- `workflow.yaml` — workflow definition (one step, no LLM)
- `run.py` — loads + executes it via the public library API
- `state.db` — created on first run (SQLite state store)
- `runs.json` — created on first run (run history)
