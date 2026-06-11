"""Example 5 — Multi-tenant dashboard demo.

Run the server in one terminal:

    .venv/bin/agentforge serve --port 8766 --log-level INFO

Then in another:

    .venv/bin/agentforge tenants add acme
    .venv/bin/agentforge tenants add tinyco
    .venv/bin/agentforge tenants set-plan acme pro
    .venv/bin/agentforge tenants set-plan tinyco free

    xdg-open http://127.0.0.1:8766/dashboard/login

    .venv/bin/agentforge run examples/05-multi-tenant-dashboard/workflow.yaml \\
        --tenant acme --agent demo --mailbox ./mailbox

This file is a doc-style helper that prints the commands.
"""

from __future__ import annotations

from pathlib import Path

CMDS = """
agentforge serve --port 8766 --log-level INFO &

agentforge tenants add acme
agentforge tenants add tinyco
agentforge tenants set-plan acme pro
agentforge tenants set-plan tinyco free

# Open in browser
xdg-open http://127.0.0.1:8766/dashboard/login
# Default admin token: "admin"  (set AGENTFORGE_ADMIN_TOKEN to change)

# Trigger a workflow run against a tenant
agentforge run examples/05-multi-tenant-dashboard/workflow.yaml \\
    --tenant acme --agent demo --mailbox ./mailbox
"""


def main() -> None:
    here = Path(__file__).resolve().parent
    print("Example 5 — multi-tenant dashboard demo\n")
    print("Run these commands in two terminals:\n")
    print(CMDS)
    print(f"\nWorkflow file: {here / 'workflow.yaml'}")


if __name__ == "__main__":
    main()
