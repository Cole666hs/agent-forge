"""Plan tiers and their monthly token limits. Single source of truth."""
from __future__ import annotations

from enum import Enum
from typing import Optional


class Plan(str, Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"


# Monthly token limits per plan. None = unlimited.
PLAN_LIMITS: dict[Plan, Optional[int]] = {
    Plan.FREE: 100_000,
    Plan.PRO: 10_000_000,
    Plan.ENTERPRISE: None,
}


def is_valid_plan(value: object) -> bool:
    """True iff `value` is a known plan tier string."""
    if not isinstance(value, str):
        return False
    return value in {p.value for p in Plan}
