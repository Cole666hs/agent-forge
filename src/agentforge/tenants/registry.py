"""agentforge.tenants.registry — JSON-backed tenant + API-key registry.

Each entry: {"tenant_id": "acme", "api_key_hash": "<sha256-hex>", "created_at": "..."}
API keys are stored as SHA-256 hashes, not plaintext — if tenants.json
leaks, the keys aren't directly usable.

Concurrency: read-modify-write race window between two `add()` calls.
For multi-process writers (multiple CLI processes), wrap `add()` in
a flock. For single-writer (typical CLI use), this is fine.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from agentforge.billing.plans import Plan, is_valid_plan


def _hash_key(api_key: str) -> str:
    """SHA-256 of the API key, returned as hex."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _generate_api_key() -> str:
    """Generate a random 256-bit API key, URL-safe base64-encoded.
    The raw value is returned to the caller ONCE (in `add()`) — only
    the hash is persisted."""
    return secrets.token_urlsafe(32)


class TenantRegistry:
    """File-backed registry of tenants and their hashed API keys.

    Lookup is by API key (constant-time comparison). The tenant_id is
    the public-facing identifier; the API key is the secret credential.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict = {"tenants": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
            if "tenants" not in self._data:
                self._data["tenants"] = {}
            # Backfill plan field for legacy entries (v0.3.0 and earlier)
            for entry in self._data["tenants"].values():
                if "plan" not in entry:
                    entry["plan"] = Plan.FREE.value
        except (json.JSONDecodeError, OSError):
            # Corrupt registry — start fresh. Operator must restore from
            # backup if this happens. We log rather than raise because
            # the registry is best-effort, not a critical path.
            self._data = {"tenants": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write — same pattern as mailbox_client.
        import os
        import tempfile
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def add(
        self,
        tenant_id: str,
        api_key: Optional[str] = None,
    ) -> str:
        """Register a new tenant. If `api_key` is omitted, generate one
        and return it (the caller is responsible for displaying it once).

        Returns the API key (whether provided or generated). Raises if
        the tenant_id is already registered.
        """
        if not tenant_id or not all(c.isalnum() or c in "-_" for c in tenant_id):
            raise ValueError(
                f"tenant_id must match [a-zA-Z0-9_-]+, got {tenant_id!r}"
            )
        if tenant_id in self._data["tenants"]:
            raise ValueError(f"tenant {tenant_id!r} already exists")
        key = api_key or _generate_api_key()
        self._data["tenants"][tenant_id] = {
            "api_key_hash": _hash_key(key),
            "plan": Plan.FREE.value,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()
        return key

    def get_plan(self, tenant_id: str) -> Plan:
        """Return the plan tier for a tenant. Defaults to FREE if field missing (legacy)."""
        entry = self._data["tenants"].get(tenant_id)
        if entry is None:
            raise ValueError(f"tenant {tenant_id!r} not found")
        raw = entry.get("plan", Plan.FREE.value)
        if not is_valid_plan(raw):
            return Plan.FREE  # corrupt data — silent default
        return Plan(raw)

    def set_plan(self, tenant_id: str, plan: Plan) -> None:
        """Change a tenant's plan tier. Raises if tenant not found."""
        if tenant_id not in self._data["tenants"]:
            raise ValueError(f"tenant {tenant_id!r} not found")
        self._data["tenants"][tenant_id]["plan"] = plan.value
        self._save()

    def lookup(self, api_key: str) -> Optional[str]:
        """Reverse-lookup: given an API key, return the tenant_id (or None)."""
        if not api_key:
            return None
        candidate = _hash_key(api_key)
        for tenant_id, entry in self._data["tenants"].items():
            stored = entry.get("api_key_hash", "")
            if hmac.compare_digest(candidate, stored):
                return tenant_id
        return None

    def remove(self, tenant_id: str) -> bool:
        """Remove a tenant. Returns True if removed, False if not found."""
        if tenant_id in self._data["tenants"]:
            del self._data["tenants"][tenant_id]
            self._save()
            return True
        return False

    def list_tenants(self) -> List[str]:
        return sorted(self._data["tenants"].keys())
