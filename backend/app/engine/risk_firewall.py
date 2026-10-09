"""Central Risk Firewall + rule-based Slippage/Latency Guard.

Every order passes through RiskFirewall.evaluate() - it is invoked from inside OrderEngine.submit(),
which is the only code path that can reach the BrokerAdapter. Strategies only emit signals.

Each check reports rule, actual value, allowed value and a human-readable message.
"""
from __future__ import annotations

import math
import time
from decimal import Decimal
from typing import TYPE_CHECKING

from app.core.events import Event
from app.database.models import RiskEvent
from app.engine.controller import RUNNING, SAFE_MODE
from app.engine.types import OrderIntent, RiskCheck, RiskDecision

if TYPE_CHECKING:
    from app.engine.system import TradingSystem

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


def inr(x: Decimal | float) -> str:
    """₹ with Indian digit grouping, e.g. ₹10,00,000."""
    n = int(Decimal(x).to_integral_value())
    sign, s = ("-" if n < 0 else ""), str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return f"{sign}₹{s}"


class LatencyGuard:
    """Transparent rules - no ML. Returns checks and possibly a safer order type."""

    def __init__(self, system: "TradingSystem"):
        self.sys = system

    def volatility_bps(self, symbol: str) -> float | None:
        bars = self.sys.market.candles.candles(symbol, "1m", limit=21, include_working=False)
        if len(bars) < 6:
            return None
        rets = [math.log(float(b.close) / float(a.close)) for a, b in zip(bars, bars[1:]) if a.close > 0]
        if len(rets) < 5:
            return None
        mean = sum(rets) / len(rets)
        return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * 10000

    def participation_pct(self, symbol: str, qty: Decimal) -> float | None:
        bars = self.sys.market.candles.candles(symbol, "1m", limit=1, include_working=False)
        if not bars or bars[-1].volume <= 0:
            return None
        return float(qty / bars[-1].volume * 100)

    def evaluate(self, intent: OrderIntent, ltp: Decimal | None) -> tuple[list[RiskCheck], str, Decimal | None]:
        lim = self.sys.limits
        checks: list[RiskCheck] = []
        order_type, limit_price = intent.order_type, intent.limit_price
        lat = self.sys.broker.current_latency()
        block, warn = float(lim.g("latency_block_ms")), float(lim.g("latency_warn_ms"))
        protect_reasons = []
        if lat is None:
            checks.append(RiskCheck("LATENCY_GUARD", "Broker API latency", WARN, "n/a", f"< {block:.0f} ms",
                                    "no latency samples yet"))
        elif lat >= block:
            checks.append(RiskCheck("HIGH_LATENCY", "Broker API latency", FAIL, f"{lat:.0f} ms", f"< {block:.0f} ms",
                                    f"HIGH LATENCY: API latency {lat / 1000:.1f}s - order temporarily blocked"))
        elif lat >= warn:
            protect_reasons.append(f"latency {lat:.0f} ms")
            checks.append(RiskCheck("LATENCY_GUARD", "Broker API latency", WARN, f"{lat:.0f} ms", f"< {warn:.0f} ms",
                                    "elevated latency - protective LIMIT order used"))
        else:
            checks.append(RiskCheck("LATENCY_GUARD", "Broker API latency", PASS, f"{lat:.0f} ms", f"< {warn:.0f} ms"))

        vol = self.volatility_bps(intent.symbol)
        vmax = float(lim.g("max_volatility_bps"))
        if vol is not None and vol > vmax:
            protect_reasons.append(f"volatility {vol:.0f} bps")
            checks.append(RiskCheck("VOLATILITY_GUARD", "1m volatility", WARN, f"{vol:.1f} bps", f"<= {vmax:.0f} bps",
                                    "high volatility - protective LIMIT order used"))
        else:
            checks.append(RiskCheck("VOLATILITY_GUARD", "1m volatility", PASS,
                                    f"{vol:.1f} bps" if vol is not None else "n/a", f"<= {vmax:.0f} bps"))

        part = self.participation_pct(intent.symbol, intent.quantity)
        pmax = float(lim.g("max_participation_pct"))
        if part is not None and part > pmax and intent.origin != "KILL_SWITCH":
            checks.append(RiskCheck("LOW_LIQUIDITY", "Order size vs 1m volume", FAIL, f"{part:.1f}%", f"<= {pmax:.0f}%",
                                    "order too large for current liquidity"))
        else:
            checks.append(RiskCheck("LIQUIDITY_GUARD", "Order size vs 1m volume", PASS,
                                    f"{part:.2f}%" if part is not None else "n/a", f"<= {pmax:.0f}%"))

        slip = self.sys.orders.recent_slippage_bps()
        smax = float(lim.g("slippage_warn_bps"))
        if slip is not None and slip > smax:
            protect_reasons.append(f"recent slippage {slip:.1f} bps")
            checks.append(RiskCheck("SLIPPAGE_GUARD", "Recent avg slippage", WARN, f"{slip:.1f} bps", f"<= {smax:.0f} bps",
                                    "slippage elevated - protective LIMIT order used"))
        else:
            checks.append(RiskCheck("SLIPPAGE_GUARD", "Recent avg slippage", PASS,
                                    f"{slip:.1f} bps" if slip is not None else "n/a", f"<= {smax:.0f} bps"))

        if protect_reasons and order_type == "MARKET" and ltp is not None and intent.origin != "KILL_SWITCH":
            band = Decimal("0.001")  # 0.1% protection band
            order_type = "LIMIT"
            limit_price = (ltp * (1 + band) if intent.side == "BUY" else ltp * (1 - band)).quantize(Decimal("0.05"))
        return checks, order_type, limit_price


EMERGENCY_ORIGINS = ("KILL_SWITCH", "CIRCUIT_BREAKER")


class RiskFirewall:
    def __init__(self, system: "TradingSystem"):
        self.sys = system
        self.guard = LatencyGuard(system)
        self.recent_identical: dict[tuple, float] = {}
        self.stats = {"approved": 0, "rejected": 0}

    async def evaluate(self, intent: OrderIntent, order_id: str | None = None) -> RiskDecision:
        sys, lim = self.sys, self.sys.limits
        # risk-reducing emergency closes (kill switch, circuit-breaker flatten) pass the gates that would trap a position
        emergency = intent.origin in EMERGENCY_ORIGINS and intent.reduce_only
        reduce = intent.reduce_only
        checks: list[RiskCheck] = []
        add = checks.append
        ctl = sys.controller
        rt = sys.strategies.runtimes.get(intent.strategy_id)
        pos = sys.positions.get(intent.strategy_id, intent.symbol)
        ltp = sys.market.ltp(intent.symbol)
        price = intent.limit_price or ltp
        delta = intent.quantity if intent.side == "BUY" else -intent.quantity
        skip = "risk-reducing emergency order"

        # ---------------- global gates
        if ctl.status == SAFE_MODE and not emergency:
            add(RiskCheck("KILL_SWITCH", "Kill switch", FAIL, "ACTIVE", "INACTIVE", "Emergency kill switch is active - SAFE MODE"))
        else:
            add(RiskCheck("KILL_SWITCH", "Kill switch", PASS, "ACTIVE (emergency close)" if emergency else "INACTIVE", "INACTIVE"))
        if emergency:
            add(RiskCheck("TRADING_STATUS", "Global trading status", SKIP, ctl.status, RUNNING, skip))
        elif ctl.status != RUNNING:
            add(RiskCheck("TRADING_STATUS", "Global trading status", FAIL, ctl.status, RUNNING, f"trading is {ctl.status}"))
        else:
            add(RiskCheck("TRADING_STATUS", "Global trading status", PASS, ctl.status, RUNNING))
        if ctl.blocks and not emergency:
            add(RiskCheck("SYSTEM_STATE", "Critical state certainty", FAIL, "; ".join(ctl.blocks.values()), "no blocks",
                          "new orders blocked until state is certain: " + "; ".join(ctl.blocks.values())))
        else:
            add(RiskCheck("SYSTEM_STATE", "Critical state certainty", SKIP if emergency and ctl.blocks else PASS,
                          "blocked" if ctl.blocks else "certain", "no blocks", skip if ctl.blocks else ""))
        if not sys.broker.available:
            add(RiskCheck("BROKER_HEALTH", "Broker connectivity", FAIL, "UNAVAILABLE", "CONNECTED",
                          f"broker unavailable: {sys.broker.last_error}"))
        else:
            add(RiskCheck("BROKER_HEALTH", "Broker connectivity", PASS, "CONNECTED", "CONNECTED"))

        # ---------------- strategy gates
        if rt is None:
            add(RiskCheck("STRATEGY_STATUS", "Strategy enabled", FAIL, "unknown", "RUNNING", "unknown strategy"))
        elif emergency:
            add(RiskCheck("STRATEGY_STATUS", "Strategy enabled", SKIP, rt.status, "RUNNING", skip))
        elif rt.status != "RUNNING":
            add(RiskCheck("STRATEGY_STATUS", "Strategy enabled", FAIL, rt.status, "RUNNING",
                          f"strategy {rt.name} is {rt.status}" + (f" ({rt.status_reason})" if rt.status_reason else "")))
        else:
            add(RiskCheck("STRATEGY_STATUS", "Strategy enabled", PASS, rt.status, "RUNNING"))

        # ---------------- market data
        fresh, age, why = sys.market.freshness(intent.symbol)
        if not fresh and not emergency:
            add(RiskCheck("STALE_MARKET_DATA", "Market data freshness", FAIL, why, f"<= {sys.market.stale_after:.0f}s old",
                          f"market data not trustworthy: {why}"))
        else:
            add(RiskCheck("MARKET_DATA", "Market data freshness", PASS if fresh else SKIP,
                          f"{age:.1f}s old" if age is not None else why, f"<= {sys.market.stale_after:.0f}s old"))

        # ---------------- duplicates / pending orders
        pending = [o for o in sys.orders.open_orders.values()
                   if o.strategy_id == intent.strategy_id and o.symbol == intent.symbol and o.order_id != order_id]
        if pending:
            add(RiskCheck("DUPLICATE_ORDER", "Pending order protection", FAIL, f"{len(pending)} open order(s)", "0",
                          f"strategy already has an open {pending[0].side} order on {intent.symbol} ({pending[0].status})"))
        else:
            window = float(lim.g("duplicate_window_s"))
            key = (intent.strategy_id, intent.symbol, intent.side, str(intent.quantity))
            last = self.recent_identical.get(key)
            if last and time.monotonic() - last < window and not emergency:
                add(RiskCheck("DUPLICATE_ORDER", "Identical order suppression", FAIL,
                              f"{time.monotonic() - last:.1f}s since identical order", f">= {window:.0f}s",
                              "identical order submitted moments ago - possible duplicate"))
            else:
                add(RiskCheck("DUPLICATE_ORDER", "Duplicate protection", PASS, "none", "none"))

        # ---------------- rate limits (every attempt counts)
        if emergency:
            add(RiskCheck("ORDER_RATE", "Order rate limits", SKIP, "-", "-", skip))
        else:
            s_cnt = await sys.redis.count_in_window(f"orders:{intent.strategy_id}", 60, add=True)
            g_cnt = await sys.redis.count_in_window("orders:global", 60, add=True)
            s_max, g_max = int(lim.s(intent.strategy_id, "max_orders_per_min")), int(lim.g("max_orders_per_min"))
            add(RiskCheck("STRATEGY_ORDER_RATE", "Strategy order rate", FAIL if s_cnt > s_max else PASS,
                          f"{s_cnt}/min", f"{s_max}/min", "strategy order rate exceeded" if s_cnt > s_max else ""))
            add(RiskCheck("GLOBAL_ORDER_RATE", "Global order rate", FAIL if g_cnt > g_max else PASS,
                          f"{g_cnt}/min", f"{g_max}/min", "global order rate exceeded" if g_cnt > g_max else ""))

        # ---------------- reduce-only validity
        if reduce:
            ok = (pos.quantity > 0 and intent.side == "SELL" and intent.quantity <= pos.quantity) or \
                 (pos.quantity < 0 and intent.side == "BUY" and intent.quantity <= -pos.quantity)
            add(RiskCheck("REDUCE_ONLY", "Reduce-only validity", PASS if ok else FAIL,
                          f"{intent.side} {intent.quantity} vs position {pos.quantity}", "must reduce position",
                          "" if ok else "reduce-only order would increase or flip the position"))

        # ---------------- exposure / margin / capital (risk-increasing orders only)
        snap = sys.pnl.compute()
        port = snap["portfolio"]
        st = snap["strategies"].get(intent.strategy_id)
        if reduce or price is None or st is None:
            why_skip = "risk-reducing order" if reduce else "no price/strategy"
            for rule, label in (("MAX_POSITION", "Strategy max position"), ("STRATEGY_MARGIN", "Virtual margin (allocation)"),
                                ("STRATEGY_EXPOSURE", "Strategy exposure"), ("GLOBAL_EXPOSURE", "Overall exposure"),
                                ("AVAILABLE_CAPITAL", "Available capital"), ("STRATEGY_DAILY_LOSS", "Strategy daily loss limit"),
                                ("DAILY_LOSS", "Daily loss limit")):
                if price is None and not reduce:
                    add(RiskCheck(rule, label, FAIL, "no price", "-", "cannot size risk without a market price"))
                else:
                    add(RiskCheck(rule, label, SKIP, "-", "-", why_skip))
        else:
            new_pos = pos.quantity + delta
            max_pos = lim.s(intent.strategy_id, "max_position")
            add(RiskCheck("MAX_POSITION", "Strategy max position", FAIL if abs(new_pos) > max_pos else PASS,
                          f"{abs(new_pos):,.0f}", f"{max_pos:,.0f}",
                          f"Maximum Position Size Exceeded - requested position {abs(new_pos):,.0f} shares, allowed {max_pos:,.0f}"
                          if abs(new_pos) > max_pos else ""))
            order_margin = intent.quantity * price * sys.settings.margin_rate
            used = st["margin_used"] + st["reserved_margin"]
            alloc = st["allocation"]
            add(RiskCheck("STRATEGY_MARGIN", "Virtual margin (allocation)", FAIL if used + order_margin > alloc else PASS,
                          inr(used + order_margin), inr(alloc),
                          "the strategy has no capital allocated - set its allocation (Strategies → Configure)" if alloc <= 0 else
                          f"order needs {inr(order_margin)} margin but only {inr(alloc - used)} of the strategy's "
                          f"{inr(alloc)} allocation is free" if used + order_margin > alloc else ""))
            notional = intent.quantity * price
            s_exp_max = lim.s(intent.strategy_id, "max_exposure")
            s_exp = st["exposure"] + notional
            add(RiskCheck("STRATEGY_EXPOSURE", "Strategy exposure", FAIL if s_exp > s_exp_max else PASS,
                          inr(s_exp), inr(s_exp_max), "strategy exposure limit exceeded" if s_exp > s_exp_max else ""))
            g_exp = port["exposure"] + notional
            g_max = lim.g("max_gross_exposure")
            add(RiskCheck("GLOBAL_EXPOSURE", "Overall exposure", FAIL if g_exp > g_max else PASS, inr(g_exp), inr(g_max),
                          "overall exposure limit exceeded" if g_exp > g_max else ""))
            avail = port["available_capital"]
            add(RiskCheck("AVAILABLE_CAPITAL", "Available capital", FAIL if order_margin > avail else PASS,
                          inr(order_margin), inr(avail), "insufficient available capital" if order_margin > avail else ""))
            s_loss = -min(st.get("today_pnl", Decimal(0)), Decimal(0))
            s_max_loss = lim.daily_loss_limit(intent.strategy_id, st["allocation"])
            add(RiskCheck("STRATEGY_DAILY_LOSS", "Strategy daily loss limit", FAIL if s_loss >= s_max_loss else PASS,
                          inr(s_loss), inr(s_max_loss),
                          "strategy's maximum loss for the day reached - only risk-reducing orders allowed"
                          if s_loss >= s_max_loss else ""))
            loss = -min(port["today_pnl"], Decimal(0))
            max_loss = lim.g("max_daily_loss")
            add(RiskCheck("DAILY_LOSS", "Daily loss limit", FAIL if loss >= max_loss else PASS, inr(loss), inr(max_loss),
                          "maximum daily loss reached - only risk-reducing orders allowed" if loss >= max_loss else ""))

        # ---------------- slippage / latency guard
        g_checks, order_type, limit_price = self.guard.evaluate(intent, ltp)
        if emergency:
            g_checks = [RiskCheck(c.rule, c.label, WARN if c.failed else c.result, c.actual, c.allowed,
                                  (c.message + " (not blocking emergency close)") if c.failed else c.message) for c in g_checks]
        checks.extend(g_checks)

        approved = not any(c.failed for c in checks)
        decision = RiskDecision(approved, checks, order_type, limit_price, ltp)
        if approved:
            self.recent_identical[(intent.strategy_id, intent.symbol, intent.side, str(intent.quantity))] = time.monotonic()
        await self._record(intent, order_id, decision)
        return decision

    async def _record(self, intent: OrderIntent, order_id: str | None, d: RiskDecision) -> None:
        f = d.primary_failure
        self.stats["approved" if d.approved else "rejected"] += 1
        msg = (f"APPROVED {intent.side} {intent.quantity} {intent.symbol}" if d.approved
               else f"REJECTED {intent.side} {intent.quantity} {intent.symbol}: {f.message or f.label}")
        warn = [c for c in d.checks if c.result == WARN]
        async with self.sys.db.transaction() as s:
            s.add(RiskEvent(strategy_id=intent.strategy_id, order_id=order_id, correlation_id=intent.correlation_id,
                            decision="APPROVED" if d.approved else "REJECTED", rule=f.rule if f else (warn[0].rule if warn else None),
                            actual=f.actual if f else None, allowed=f.allowed if f else None, message=msg,
                            checks=[c.to_dict() for c in d.checks]))
        self.sys.health.beat("risk_firewall", detail=f"{self.stats['approved']} approved / {self.stats['rejected']} rejected")
        await self.sys.bus.publish(Event("risk.decision", "RiskFirewall", correlation_id=intent.correlation_id,
                                         strategy_id=intent.strategy_id, order_id=order_id, symbol=intent.symbol,
                                         payload={"message": msg, "origin": intent.origin, **d.to_dict()}))
        if not d.approved and intent.origin in ("STRATEGY", "RUNAWAY", "FAULT_LAB", "DEMO"):
            await self._circuit_breaker(intent)

    async def _circuit_breaker(self, intent: OrderIntent) -> None:
        cnt = await self.sys.redis.count_in_window(f"rejects:{intent.strategy_id}", 60, add=True)
        mx = int(self.sys.limits.g("max_rejections_per_min"))
        rt = self.sys.strategies.runtimes.get(intent.strategy_id)
        if cnt >= mx and rt is not None and rt.status == "RUNNING":
            reason = f"circuit breaker: {cnt} risk rejections in 60s (limit {mx}) - possible runaway strategy"
            await self.sys.strategies.set_status(intent.strategy_id, "HALTED", reason, actor="RiskFirewall",
                                                 correlation_id=intent.correlation_id)
            # flatten in the background: this runs inside an order submission that holds the strategy's order lock
            self.sys.strategies.start_flatten(intent.strategy_id, reason, intent.correlation_id)
