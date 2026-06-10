"""Tests for billing.usage — per-tenant per-month token counter."""
import json
import pytest
from unittest.mock import patch
from agentforge.billing.usage import UsageStore


def _current_month() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m")


def test_initial_usage_is_zero(tmp_path):
    s = UsageStore(path=tmp_path / "usage.json")
    u = s.get("acme")
    assert u.tokens == 0
    assert u.month == _current_month()


def test_record_tokens_accumulates(tmp_path):
    s = UsageStore(path=tmp_path / "usage.json")
    s.record("acme", 100)
    s.record("acme", 50)
    assert s.get("acme").tokens == 150


def test_get_unknown_tenant_returns_zero(tmp_path):
    s = UsageStore(path=tmp_path / "usage.json")
    assert s.get("ghost").tokens == 0


def test_persistence_across_instances(tmp_path):
    p = tmp_path / "usage.json"
    s1 = UsageStore(path=p)
    s1.record("acme", 500)
    s2 = UsageStore(path=p)
    assert s2.get("acme").tokens == 500


def test_month_rollover_resets_counter(tmp_path):
    p = tmp_path / "usage.json"
    s = UsageStore(path=p)
    s.record("acme", 1000)
    with patch.object(UsageStore, "_current_month", return_value="2099-12"):
        u = s.get("acme")
        assert u.tokens == 0
        assert u.month == "2099-12"


def test_legacy_file_loads_then_resets_to_current_month(tmp_path):
    p = tmp_path / "usage.json"
    p.write_text(json.dumps({"tenants": {"acme": {"tokens": 42, "month": "2020-01"}}}))
    s = UsageStore(path=p)
    # Different month on disk than now → resets to 0 in current month
    assert s.get("acme").tokens == 0


def test_record_after_rollover_starts_fresh(tmp_path):
    p = tmp_path / "usage.json"
    s = UsageStore(path=p)
    s.record("acme", 1000)
    with patch.object(UsageStore, "_current_month", return_value="2099-12"):
        s.record("acme", 200)
        assert s.get("acme").tokens == 200


def test_atomic_write_no_partial_file_on_crash(tmp_path):
    p = tmp_path / "usage.json"
    s = UsageStore(path=p)
    s.record("acme", 100)
    original = p.read_text()
    with patch("os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            s.record("acme", 50)
    assert p.read_text() == original


def test_record_rejects_negative(tmp_path):
    s = UsageStore(path=tmp_path / "usage.json")
    with pytest.raises(ValueError, match="non-negative"):
        s.record("acme", -1)


def test_reset_clears_tenant(tmp_path):
    s = UsageStore(path=tmp_path / "usage.json")
    s.record("acme", 100)
    s.reset("acme")
    assert s.get("acme").tokens == 0
