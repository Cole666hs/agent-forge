"""Example 7 — Workflow versioning with the v0.14.0 store.

Pure-Python walkthrough of the SQLiteWorkflowVersionStore. No LLM, no
adapter, no daemon. The point is to show the API surface end-to-end so a
user can plug it into their own CI/release flow.

The example:
  1. Creates a SQLite state.db in this directory
  2. Saves three versions of `demo-workflow` (v1, v2, v3) with progressively
     different YAML
  3. Lists them, newest first
  4. Diffs v1 against v3
  5. Shows the YAML content of v1
  6. Restores v1 (writes v1's YAML to `restored-workflow.yaml`) and
     confirms the new version is also stored

Run it:

    .venv/bin/python examples/07-workflow-versioning/run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from agentforge.state import State  # noqa: E402

V1 = """\
name: demo-workflow
description: v1 — first draft, single echo step.
steps:
  - id: capture
    type: record
    inputs:
      output_key: echo
      message: "{{ inputs.text }}"
"""

V2 = """\
name: demo-workflow
description: v2 — added a second step that lower-cases the echo.
steps:
  - id: capture
    type: record
    inputs:
      output_key: echo
      message: "{{ inputs.text }}"
  - id: lower
    type: record
    inputs:
      output_key: echo
      message: "{{ echo | lower }}"
"""

V3 = """\
name: demo-workflow
description: v3 — added a third step that upper-cases the echo (oops!).
steps:
  - id: capture
    type: record
    inputs:
      output_key: echo
      message: "{{ inputs.text }}"
  - id: lower
    type: record
    inputs:
      output_key: echo
      message: "{{ echo | lower }}"
  - id: upper
    type: record
    inputs:
      output_key: echo
      message: "{{ echo | upper }}"
"""


def main() -> None:
    here = Path(__file__).resolve().parent
    state_db = here / "state.db"
    if state_db.exists():
        state_db.unlink()
    out = here / "restored-workflow.yaml"

    state = State(state_db)
    store = state.workflows  # SQLiteWorkflowVersionStore
    name = "demo-workflow"

    print(f"== Saving three versions of {name!r} ==")
    h1 = state.workflows.save_version(name, V1)
    h2 = state.workflows.save_version(name, V2)
    h3 = state.workflows.save_version(name, V3)
    print(f"  v1: {h1}")
    print(f"  v2: {h2}")
    print(f"  v3: {h3}")

    print("\n== List (newest first) ==")
    for v in state.workflows.list_versions(name):
        print(f"  {v.version_hash}  {v.saved_at}  ({len(v.content)} chars)")

    print("\n== Diff v1 -> v3 ==")
    diff = state.workflows.diff(name, h1, h3)
    print(diff or "  (no diff)")

    print(f"\n== Show v1 yaml ({h1[:12]}…) ==")
    v1_obj = state.workflows.get_version(name, h1)
    assert v1_obj is not None
    print(v1_obj.content)

    print(f"== Restore v1 to {out.name} ==")
    out.write_text(v1_obj.content)
    restored_hash = state.workflows.save_version(name, v1_obj.content)
    print(f"  wrote {out} ({out.stat().st_size} bytes)")
    print(f"  new restore-version: {restored_hash}")

    print("\n== Final history ==")
    for v in state.workflows.list_versions(name):
        print(f"  {v.version_hash}  {v.saved_at}")

    state.close()
    print("\ndone.")


if __name__ == "__main__":
    main()
