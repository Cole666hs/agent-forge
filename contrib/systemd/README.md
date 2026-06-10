# agentforge — Systemd Deployment

## Quick start

```bash
# 1. Install
sudo install -d /opt/agentforge /var/lib/agentforge/mailbox
sudo install -d -o agentforge -g agentforge /var/lib/agentforge
cd /opt/agentforge && uv venv .venv
cd /opt/agentforge && uv pip install -e .

# 2. Create your workflow
agentforge init mybot
mv mybot/* /opt/agentforge/

# 3. Configure env
sudo install -d /etc/agentforge
sudo cp .env.example /etc/agentforge/agentforge.env
sudoedit /etc/agentforge/agentforge.env  # fill in API keys

# 4. Start a per-agent instance
sudo systemctl enable --now agentforge@mybot.service
journalctl -u agentforge@mybot.service -f
```

## Per-agent instances

The unit file uses `%i` (instance name) so you can run one daemon per agent:

```bash
agentforge@bot1.service
agentforge@bot2.service
agentforge@relay.service
```

Each instance has its own inbox (`--agent <name>`) but shares the same
mailbox root (`/var/lib/agentforge/mailbox`) so agents can message each
other across instances.

## Hardening

The unit follows the systemd hardening recommendations:
- `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`
- `ReadWritePaths` restricted to the mailbox dir
- `RestrictAddressFamilies` allows only Unix + TCP sockets
- `LockPersonality`, `RestrictNamespaces`, `RestrictRealtime`

This is the same hardening profile as `mailbox-llm-bridge` on HAMILLER
(verified 04.06.2026). For details on the rationale, see the
`devops/systemd-service-hardened` skill.

## Operational notes

- The daemon runs `agentforge run --watch`, polling the inbox every 5s.
  On WorkflowError it logs and continues — one bad message doesn't kill
  the service.
- State persistence: if you add `--state-db /var/lib/agentforge/state.db`
  to the ExecStart line, workflow state survives crashes.
- Rotate `journalctl -u agentforge@<name>.service --vacuum-time=7d` to
  keep logs bounded.
