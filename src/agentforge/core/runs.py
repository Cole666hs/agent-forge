"""Per-workflow run history, JSON-backed.

Each run is a single record (start, end, status, duration, optional error).
The store keeps the most recent N runs per workflow (default 100) — old
runs are evicted on insert. Writes are atomic (tempfile + os.replace).
Reads return newest first.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class RunRecord:
    """One execution of a workflow."""
    id: str
    workflow: str
    tenant_id: str
    agent: str
    started_at: str   # ISO 8601
    ended_at: str     # ISO 8601
    status: str       # success | error
    duration_seconds: float
    error: Optional[str]


class RunStore:
    """JSON-backed run history with per-workflow cap.

    Schema: {"runs": [{...RunRecord...}, ...]}
    Runs are ordered by insertion (not by timestamp) — the newest run is
    appended last. list_runs() reverses for newest-first display.
    """

    def __init__(self, path: Path, max_per_workflow: int = 100):
        self.path = Path(path)
        self.max_per_workflow = max_per_workflow
        self._data: dict = {"runs": []}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
            if "runs" not in self._data:
                self._data = {"runs": []}
        except (json.JSONDecodeError, OSError):
            self._data = {"runs": []}

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

    def record(self, run: RunRecord) -> None:
        """Append a run. Evicts oldest runs for the same workflow if cap exceeded."""
        self._data["runs"].append(asdict(run))
        # Evict oldest beyond the cap, per workflow
        by_wf: dict[str, list[dict]] = {}
        for r in self._data["runs"]:
            by_wf.setdefault(r["workflow"], []).append(r)
        trimmed: list[dict] = []
        for wf_runs in by_wf.values():
            if len(wf_runs) > self.max_per_workflow:
                wf_runs = wf_runs[-self.max_per_workflow:]
            trimmed.extend(wf_runs)
        self._data["runs"] = trimmed
        self._save()

    def list_runs(self, workflow: str, limit: Optional[int] = None) -> List[RunRecord]:
        """Return newest-first list of runs for one workflow, optionally capped."""
        wf_runs = [r for r in self._data["runs"] if r["workflow"] == workflow]
        wf_runs.reverse()  # newest first
        if limit is not None:
            wf_runs = wf_runs[:limit]
        return [RunRecord(**r) for r in wf_runs]
