"""Per-tenant per-month token usage tracking, JSON-backed."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class TokenUsage:
    """Snapshot of one tenant's token usage for the current calendar month."""
    tenant_id: str
    tokens: int
    month: str  # "YYYY-MM" (UTC)


class UsageStore:
    """JSON-backed per-tenant per-month token counter.

    On read, if the stored month doesn't match the current UTC month,
    the entry is treated as 0 in the current month (lazy reset). Writes
    are atomic via tempfile + os.replace (same pattern as tenants.json
    and FileMailbox).
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict = {"tenants": {}}
        self._load()

    @staticmethod
    def _current_month() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
            if "tenants" not in self._data:
                self._data = {"tenants": {}}
        except (json.JSONDecodeError, OSError):
            # Corrupt file — start fresh. Operator must restore from backup.
            self._data = {"tenants": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    def get(self, tenant_id: str) -> TokenUsage:
        """Return current-month usage. Lazy reset on month change."""
        month = self._current_month()
        entry = self._data["tenants"].get(tenant_id)
        if entry is None or entry.get("month") != month:
            return TokenUsage(tenant_id=tenant_id, tokens=0, month=month)
        return TokenUsage(
            tenant_id=tenant_id,
            tokens=int(entry.get("tokens", 0)),
            month=month,
        )

    def record(self, tenant_id: str, tokens: int) -> None:
        """Add `tokens` to the current-month total for `tenant_id`."""
        if tokens < 0:
            raise ValueError("tokens must be non-negative")
        month = self._current_month()
        entry = self._data["tenants"].get(tenant_id)
        if entry is None or entry.get("month") != month:
            self._data["tenants"][tenant_id] = {"tokens": tokens, "month": month}
        else:
            entry["tokens"] = int(entry.get("tokens", 0)) + tokens
        self._save()

    def reset(self, tenant_id: str) -> None:
        """Manually clear a tenant's usage (admin operation)."""
        if tenant_id in self._data["tenants"]:
            del self._data["tenants"][tenant_id]
            self._save()
