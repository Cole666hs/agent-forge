"""Tests for billing.plans — Plan enum and PLAN_LIMITS."""
from agentforge.billing.plans import Plan, PLAN_LIMITS, is_valid_plan


def test_plan_enum_values():
    assert Plan.FREE == "free"
    assert Plan.PRO == "pro"
    assert Plan.ENTERPRISE == "enterprise"


def test_plan_limits_free():
    assert PLAN_LIMITS[Plan.FREE] == 100_000


def test_plan_limits_pro():
    assert PLAN_LIMITS[Plan.PRO] == 10_000_000


def test_plan_limits_enterprise_is_unlimited():
    assert PLAN_LIMITS[Plan.ENTERPRISE] is None


def test_is_valid_plan_known_values():
    assert is_valid_plan("free") is True
    assert is_valid_plan("pro") is True
    assert is_valid_plan("enterprise") is True


def test_is_valid_plan_rejects_unknown():
    assert is_valid_plan("invalid") is False
    assert is_valid_plan("") is False
    assert is_valid_plan(None) is False
    assert is_valid_plan(42) is False
