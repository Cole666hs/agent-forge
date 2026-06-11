"""Quota computation + enforcement. Pure functions over (registry, usage)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from agentforge.billing.plans import Plan, PLAN_LIMITS
from agentforge.billing.usage import UsageStore
from agentforge.tenants.registry import TenantRegistry


WARNING_THRESHOLD = 0.8  # 80%


@dataclass(frozen=True)
class QuotaStatus:
    """Snapshot of a tenant's quota position for the current month."""
    tenant_id: str
    plan: Plan
    used: int
    limit: Optional[int]   # None = unlimited
    remaining: Optional[int]
    pct: float             # 0.0-1.0+
    warning: bool          # True at >= 80%
    exceeded: bool         # True at >= limit


class QuotaExceededError(Exception):
    """Raised by enforce_quota() when a request would push a tenant over their plan limit."""

    def __init__(self, tenant_id: str, used: int, limit: int, requested: int):
        self.tenant_id = tenant_id
        self.used = used
        self.limit = limit
        self.requested = requested
        super().__init__(
            f"quota exceeded for tenant {tenant_id!r}: "
            f"used={used}, limit={limit}, requested={requested}"
        )


def quota_status(
    registry: TenantRegistry,
    usage: UsageStore,
    tenant_id: str,
) -> QuotaStatus:
    """Compute the current quota position for a tenant."""
    plan = registry.get_plan(tenant_id)
    u = usage.get(tenant_id)
    limit = PLAN_LIMITS[plan]

    if limit is None:
        return QuotaStatus(
            tenant_id=tenant_id, plan=plan, used=u.tokens,
            limit=None, remaining=None, pct=0.0,
            warning=False, exceeded=False,
        )

    pct = u.tokens / limit if limit > 0 else 0.0
    remaining = max(0, limit - u.tokens)
    return QuotaStatus(
        tenant_id=tenant_id, plan=plan, used=u.tokens,
        limit=limit, remaining=remaining, pct=pct,
        warning=pct >= WARNING_THRESHOLD, exceeded=u.tokens >= limit,
    )


def enforce_quota(
    registry: TenantRegistry,
    usage: UsageStore,
    tenant_id: str,
    tokens_to_add: int,
) -> QuotaStatus:
    """Pre-flight check: would `tokens_to_add` push this tenant over their limit?

    If yes → raise QuotaExceededError. If no → record the tokens and return the
    post-call status. Unlimited plans always pass.
    """
    if tokens_to_add < 0:
        raise ValueError("tokens_to_add must be non-negative")

    plan = registry.get_plan(tenant_id)
    limit = PLAN_LIMITS[plan]

    if limit is None:
        # Unlimited — just record
        usage.record(tenant_id, tokens_to_add)
        return quota_status(registry, usage, tenant_id)

    current = usage.get(tenant_id).tokens
    if current + tokens_to_add > limit:
        raise QuotaExceededError(
            tenant_id=tenant_id, used=current, limit=limit, requested=tokens_to_add,
        )

    usage.record(tenant_id, tokens_to_add)
    return quota_status(registry, usage, tenant_id)
