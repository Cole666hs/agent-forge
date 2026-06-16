# Example 7 — Workflow versioning

Demonstrates the **v0.14.0** `SQLiteWorkflowVersionStore`. No LLM, no
adapter, no daemon — a self-contained script you can copy into your own
CI/release flow.

## What it shows

| Step | API call | Purpose |
|---|---|---|
| 1 | `state.workflows.save_version(name, yaml)` | Snapshot a workflow's YAML; returns the SHA-256 content hash |
| 2 | `state.workflows.list_versions(name)` | List saved versions, newest first |
| 3 | `state.workflows.diff(name, hash_a, hash_b)` | Unified diff between two versions |
| 4 | `state.workflows.get_version(name, hash)` | Fetch one version's full YAML |
| 5 | `state.workflows.save_version(name, restored_yaml)` | Restore is a new version (append-only — the old version is never deleted) |

## Run it

```bash
.venv/bin/python examples/07-workflow-versioning/run.py
```

Expected output: a short report showing the three saves, the diff, the
restored YAML, and the final history. Side effects: a local
`state.db` and `restored-workflow.yaml` in this directory (both are
gitignored — see `examples/.gitignore`).

## When to use this in real life

- **Release flow:** tag a version before a deploy; if the deploy breaks,
  the previous YAML is one `restore` away.
- **Audit:** every save has a `saved_at`, `saved_by`, and `note` column.
  Wire `saved_by` to your CI job name for a full audit trail.
- **Diff before save:** the editor can `diff` against the latest version
  before committing a new one — a poor man's PR review for workflows.
