# Deployment guide

Two supported ways to run `agentforge serve` in production: **Docker
(compose)** and **systemd (user-mode)**. Pick one — the daemon behaves
identically either way.

## TL;DR

```bash
# Docker
cp .env.example .env && $EDITOR .env
docker compose up -d
docker compose exec agentforge agentforge tenants add acme

# systemd (user-mode, no root)
uv venv .venv --python 3.11 && uv pip install --python .venv/bin/python -e .
mkdir -p ~/.config/systemd/user
cp contrib/systemd/agentforge.service ~/.config/systemd/user/
$EDITOR ~/.config/systemd/user/agentforge.service   # fix the paths
systemctl --user daemon-reload
systemctl --user enable --now agentforge
loginctl enable-linger $USER
```

After either path, the daemon listens on `http://127.0.0.1:8766`. The
dashboard is at `http://127.0.0.1:8766/dashboard`. The MCP server
(v0.11.0) is enabled by default and reachable on the same port.

## Docker (recommended for most users)

### Files

- `Dockerfile` — multi-stage-ready production image, ~150 MB compressed
- `docker-compose.yml` — single-service stack with healthcheck
- `.env.example` — annotated template; copy to `.env` and edit
- `examples/06-docker-deploy/nginx.conf` — optional reverse proxy with
  TLS placeholders

### What gets persisted

| Path inside container | Host path (volume) | Contents |
|---|---|---|
| `/app/data/state.db` | `./data/state.db` | Tenants, usage, runs, run_events, workflow_versions |
| `/app/data/mailbox/` | `./data/mailbox/` | One subdir per tenant, message files |
| `/app/workflows/` | `./workflows/` (read-only) | YAML workflow files served at `/v1/workflows/{name}/run` |

The host `./data` directory is the only thing you need to back up to
recover from a total container loss.

### Upgrades

```bash
git pull
docker compose build
docker compose up -d
```

`./data` is a bind mount, so it survives `up -d`. The state.db schema
migrates transparently (see `SCHEMA_VERSION` in `src/agentforge/state.py`).

### TLS

Put a reverse proxy in front. Two options:

1. **nginx** — `examples/06-docker-deploy/nginx.conf` is a working
   starting point. Mount your certs and uncomment the redirect block.
2. **Caddy** — one-line `reverse_proxy agentforge:8766` plus auto-TLS
   if you have a public hostname.

The daemon itself is plain HTTP. Don't expose port 8766 to the
internet without a TLS terminator in front.

### Resource limits

Default container is unbounded. For multi-tenant deployments, add
`deploy.resources.limits` to the agentforge service:

```yaml
deploy:
  resources:
    limits:
      cpus: '2.0'
      memory: 1G
    reservations:
      cpus: '0.5'
      memory: 256M
```

## systemd (recommended for bare-metal / single-host)

### Why user-mode?

agentforge is a single-tenant per-install library+daemon. It doesn't
need root, doesn't bind privileged ports, and runs fine as a long-lived
user process. User-mode systemd gives you auto-restart, journald logs,
and survives logout without sudo.

### Install (bare-metal, fresh)

```bash
# 1. Clone and install
git clone https://github.com/Cole666hs/agent-forge.git ~/Developer/agent-forge
cd ~/Developer/agent-forge
uv venv .venv --python 3.11
uv pip install --python .venv/bin/python -e .

# 2. Configure
cp .env.example .env && $EDITOR .env

# 3. Install the unit
mkdir -p ~/.config/systemd/user
cp contrib/systemd/agentforge.service ~/.config/systemd/user/
$EDITOR ~/.config/systemd/user/agentforge.service   # fix %u → your username
systemctl --user daemon-reload
systemctl --user enable --now agentforge

# 4. Survive logout
sudo loginctl enable-linger $USER
```

### Logs

```bash
journalctl --user -u agentforge -f
```

### Hardening

The shipped unit enables:

- `NoNewPrivileges` — cannot escalate
- `ProtectSystem=strict` — `/usr`, `/boot`, `/efi` read-only
- `ProtectHome=read-only` — your home is read-only except for `data/`
- `PrivateTmp` — isolated `/tmp`
- `EnvironmentFile` — secrets in `~/.config/agentforge/.env`, mode `0600`

If you need more (network namespace, syscall filter, etc.), add them
to the `[Service]` section — see `man systemd.exec`.

## Verification

After either path:

```bash
# Health
curl -s http://127.0.0.1:8766/readyz
# {"status":"ok"} → 200

# Version
agentforge --version
# agentforge, version 0.17.0

# Create a tenant
agentforge tenants add acme
# prints an API key — paste it into the dashboard login

# Run a workflow
agentforge run examples/01-hello-world/workflow.yaml --agent demo
```

## Backing up

```bash
# Just the data dir is enough.
tar -czf agentforge-backup-$(date +%F).tar.gz data/

# Restore
tar -xzf agentforge-backup-YYYY-MM-DD.tar.gz
```

The on-disk format is plain SQLite + JSON files. No proprietary
binary blobs. Restore is just a `cp -r`.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `bind: address already in use` | Another process on 8766 | `lsof -i :8766` or change port |
| `permission denied` on `/app/data` | Volume owned by root | `chown 10001:10001 ./data` |
| Dashboard login rejected | Wrong API key | Re-run `agentforge tenants add` |
| Workflows return 404 | workflows_dir not mounted | Set `AGENTFORGE_WORKFLOWS_DIR` or mount `./workflows` |
| OOM kills | Body too large | Raise `AGENTFORGE_MAX_BODY_BYTES` |
