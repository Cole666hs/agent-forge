"""Example 1 — Hello World.

The smallest possible AgentForge program: load a workflow, register a
custom step type, run it, print the result. ~40 lines of real code, no
external services required.

This example also shows the most important extensibility surface: custom
step handlers via `agentforge.workflows.engine.register_step_type`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict

# Make `python examples/01-hello-world/run.py` work from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from agentforge.core.mailbox import FileMailbox  # noqa: E402
from agentforge.workflows.engine import (  # noqa: E402
    State,
    StepContext,
    Workflow,
    register_step_type,
)


async def record_step(
    inputs: Dict[str, Any], state: State, ctx: StepContext
) -> None:
    """Write `inputs.message` (template-rendered) into `state[output_key]`.

    No mailbox or LLM side effects. Useful for "do nothing except keep
    this value" steps in your workflows.
    """
    output_key = inputs.get("output_key", "record")
    state.set(output_key, inputs.get("message", ""))


def main() -> None:
    here = Path(__file__).resolve().parent
    wf_path = here / "workflow.yaml"
    mailbox_root = here / "mailbox"
    mailbox_root.mkdir(parents=True, exist_ok=True)

    # 1. Register the custom step type (in real code: do this at import time)
    register_step_type("record", record_step)

    # 2. Load the workflow from YAML.
    wf = Workflow.from_yaml(wf_path)
    print(f"loaded workflow: {wf.name} ({len(wf.steps)} steps)")

    # 3. Build state and seed the input the workflow expects.
    state = State()
    state.set("inputs.text", "hello from agentforge")

    # 4. Run it. No LLM, no mailbox traffic — record step is self-contained.
    mbox = FileMailbox(root=mailbox_root)
    asyncio.run(wf.run(state=state, mailbox=mbox, llm=None, agent_name="hello-agent"))

    # 5. Inspect the result.
    print(f"\nstate after run:\n{json.dumps(state._data, indent=2, default=str)}")


if __name__ == "__main__":
    # Clean prior artifacts so the example is reproducible
    for f in (Path(__file__).resolve().parent / "state.db",):
        if f.exists():
            f.unlink()
    main()
