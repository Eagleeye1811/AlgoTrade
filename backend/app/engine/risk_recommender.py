"""Dynamic demographic-based risk and portfolio recommendation engine.

Calculates age-adjusted, income-aware risk capacity scores and translates them into
strictly safe Global Risk Firewall limits and per-strategy allocation splits.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any


def calculate_risk_recommendations(
    age: int = 21,
    monthly_income: Decimal | float | int = 0,
    risk_appetite: str = "AGGRESSIVE",  # AGGRESSIVE | MODERATE | CONSERVATIVE
    capital: Decimal | float | int = 1000000,
    strategy_ids: list[str] | None = None,
) -> dict[str, Any]:
    cap = Decimal(str(capital)) if capital else Decimal("1000000")
    inc = Decimal(str(monthly_income)) if monthly_income else Decimal("0")
    age = max(18, min(100, int(age)))
    appetite = str(risk_appetite).upper()
    if appetite not in ("AGGRESSIVE", "MODERATE", "CONSERVATIVE"):
        appetite = "MODERATE"

    # 1. Base Age Capacity Factor: Rule of (100 - Age)
    age_capacity = max(Decimal("0.20"), Decimal(100 - age) / Decimal("100"))

    # 2. Risk Appetite Multiplier
    appetite_mult = {
        "AGGRESSIVE": Decimal("1.25"),
        "MODERATE": Decimal("1.00"),
        "CONSERVATIVE": Decimal("0.65"),
    }[appetite]

    # 3. Income Buffer Multiplier (ability to absorb loss from cash flow)
    if inc >= Decimal("150000"):
        income_mult = Decimal("1.20")
    elif inc >= Decimal("50000"):
        income_mult = Decimal("1.05")
    elif inc > 0:
        income_mult = Decimal("0.95")
    else:
        # Student / Sandbox tester / No primary salary
        income_mult = Decimal("0.90")

    # 4. Computed Risk Score (15 to 98)
    raw_score = age_capacity * Decimal("100") * appetite_mult * income_mult
    risk_score = int(max(Decimal("15"), min(Decimal("98"), raw_score.quantize(Decimal("1")))))

    # 5. Determine Tier & Rationale
    if risk_score >= 80:
        tier = "Aggressive Alpha / Young Trader"
        loss_pct = Decimal("0.020") if age <= 25 else Decimal("0.025")
        exposure_pct = Decimal("0.50")
        orders_per_min = 30
        margin_util_target = 25.0
        rationale = (
            f"At age {age} with high risk tolerance, you can withstand controlled intraday volatility "
            f"while benefiting from multi-strategy diversification. Recommended daily loss limit is {float(loss_pct*100):.1f}% "
            f"to protect starting capital while allowing active algorithmic alpha."
        )
    elif risk_score >= 50:
        tier = "Balanced Growth & Prudence"
        loss_pct = Decimal("0.015")
        exposure_pct = Decimal("0.40")
        orders_per_min = 20
        margin_util_target = 20.0
        rationale = (
            f"At age {age} with moderate risk appetite, risk limits balance steady capital growth with strict drawdown control. "
            f"Daily loss is capped at {float(loss_pct*100):.1f}% with 40% maximum gross exposure."
        )
    else:
        tier = "Capital Preservation & Defensive"
        loss_pct = Decimal("0.005")  # 0.5% max loss
        exposure_pct = Decimal("0.25")
        orders_per_min = 10
        margin_util_target = 10.0
        rationale = (
            f"At age {age}, capital preservation is paramount. Daily loss is tightly capped at {float(loss_pct*100):.1f}% "
            f"with defensive exposure limits to safeguard wealth."
        )

    # Per-strategy max position (shares) and max orders per minute, by risk tier
    if risk_score >= 80:
        strategy_max_position, strategy_orders_pm = 50, 10
    elif risk_score >= 50:
        strategy_max_position, strategy_orders_pm = 30, 8
    else:
        strategy_max_position, strategy_orders_pm = 15, 5

    # Global Limits
    max_daily_loss = (cap * loss_pct).quantize(Decimal("100"))
    max_gross_exposure = (cap * exposure_pct).quantize(Decimal("1000"))

    # Strategy Distribution
    active_sids = strategy_ids or ["time_entry", "open_breakout", "ema_trend"]
    strategy_recs: dict[str, dict[str, Any]] = {}

    if len(active_sids) == 0:
        active_sids = ["time_entry", "open_breakout", "ema_trend"]

    # Allocatable Capital: keep 20% buffer in reserve
    allocatable_pool = (cap * Decimal("0.80")).quantize(Decimal("1000"))
    reserve_buffer = cap - allocatable_pool

    # Preset strategy weightings
    weights = {
        "time_entry": Decimal("0.25"),
        "open_breakout": Decimal("0.375"),
        "ema_trend": Decimal("0.375"),
        "rsi_ema": Decimal("0.30"),
        "macd": Decimal("0.30"),
        "breakout": Decimal("0.30"),
    }

    # Normalize weights for the active strategies
    total_w = sum(weights.get(sid, Decimal("0.33")) for sid in active_sids) or Decimal("1")

    for sid in active_sids:
        w = weights.get(sid, Decimal("0.33")) / total_w
        strat_alloc = (allocatable_pool * w).quantize(Decimal("1000"))
        # Strategy daily loss is ~3% of strategy allocation or proportional share of total loss
        strat_loss = (strat_alloc * (loss_pct * Decimal("1.5"))).quantize(Decimal("100"))
        strat_loss = min(strat_loss, max_daily_loss)
        
        # the problem statement's per-strategy limits: one size per risk tier for every strategy
        pos_size, orders_pm = strategy_max_position, strategy_orders_pm

        strategy_recs[sid] = {
            "strategy_id": sid,
            "allocation": float(strat_alloc),
            "max_daily_loss": float(strat_loss),
            "max_position": pos_size,
            "max_orders_per_min": orders_pm,
            "max_exposure": float(strat_alloc),
        }

    return {
        "profile": {
            "age": age,
            "monthly_income": float(inc),
            "risk_appetite": appetite,
            "capital": float(cap),
        },
        "risk_score": risk_score,
        "risk_tier": tier,
        "rationale": rationale,
        "global_limits": {
            "max_daily_loss": float(max_daily_loss),
            "max_daily_loss_pct": float(loss_pct * 100),
            "max_gross_exposure": float(max_gross_exposure),
            "max_gross_exposure_pct": float(exposure_pct * 100),
            "max_orders_per_min": orders_per_min,
            "target_margin_utilization_pct": margin_util_target,
        },
        # the three limits the problem statement requires, as applied to every strategy
        "strategy_limits": {"max_position": strategy_max_position, "max_orders_per_min": strategy_orders_pm},
        "safety_buffer": float(reserve_buffer),
        "strategies": strategy_recs,
    }
