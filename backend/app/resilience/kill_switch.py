"""Emergency Kill Switch - everything stopped, cancelled and flat within DEADLINE_S (10 s).

1. SAFE MODE immediately: no strategy can place an order (Risk Firewall global gate), runaway loops cancelled
2. all working orders cancelled in parallel
3. an emergency reduce-only MARKET close for every open position, in parallel, through the normal
   order/fill pipeline (each strategy's book is closed separately, so per-strategy P&L stays exact)
4. until the deadline: any position still open without a working close order (partial fill, rejected or
   failed close, a fill that arrived late) gets a new close order
5. positions still open at the deadline are reported as UNRESOLVED - never hidden; retry_close() tries again
The elapsed time is recorded so the 10-second guarantee is visible in the UI and the audit trail.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from app.core.events import Event, new_id, utcnow
from app.engine.controller import PAUSED, SAFE_MODE
from app.engine.types import OrderIntent

if TYPE_CHECKING:
    from app.engine.system import TradingSystem

log = logging.getLogger("algotrade.kill_switch")
DEADLINE_S = 10.0
CANCEL_WAIT_S = 2.5  # max wait for cancel confirmations before closing positions anyway
RECLOSE_GAP_S = 1.5  # per position: never re-close sooner (a fill's position update must land first)


class KillSwitch:
    def __init__(self, system: "TradingSystem"):
        self.sys = system
        self.state: dict[str, Any] = {"active": False}
        self._task: asyncio.Task | None = None
        self._t0 = 0.0
        self._last_close: dict[tuple[str, str], float] = {}

    async def load(self) -> None:
        st = await self.sys.state.get("kill_switch")
        if st:
            self.state = st

    async def _save(self) -> None:
        await self.sys.state.set("kill_switch", self.state)
        await self.sys.bus.publish(Event("kill_switch.update", "KillSwitch", correlation_id=self.state.get("correlation_id"),
                                         payload=self.state))

    async def activate(self, actor: str, reason: str = "manual activation", wait: bool = False,
                       deadline_s: float = DEADLINE_S) -> dict[str, Any]:
        sys = self.sys
        async with sys.redis.lock("kill_switch", timeout_s=60):
            if self.state.get("active") and self.state.get("phase") != "COMPLETED_WITH_ERRORS":
                return self.state
            self._t0 = time.monotonic()
            self._last_close = {}
            corr = new_id()
            # 1. stop everything new - first, before anything else
            await sys.controller.set_status(SAFE_MODE, f"EMERGENCY KILL SWITCH: {reason}", actor=actor)
            for rt in sys.strategies.runtimes.values():
                if rt.runaway_task:
                    rt.runaway_task.cancel()
            self.state = {"active": True, "phase": "CANCELLING", "activated_at": utcnow().isoformat(), "by": actor,
                          "reason": reason, "correlation_id": corr, "deadline_s": deadline_s, "cancelled": [],
                          "cancel_failed": [], "close_orders": [], "unresolved": [], "completed_at": None,
                          "elapsed_s": None, "within_deadline": None}
            await sys.audit.record("KILL_SWITCH_ACTIVATED", f"EMERGENCY KILL SWITCH activated by {actor}: {reason}",
                                   source="KillSwitch", severity="CRITICAL", actor=actor, correlation_id=corr)
            await self._save()

            # 2. cancel working (non-emergency) orders, all at once
            pending = [o for o in sys.orders.open_orders.values() if o.origin != "KILL_SWITCH"]
            results = await asyncio.gather(*(sys.orders.cancel(o.order_id, "cancelled by kill switch") for o in pending),
                                           return_exceptions=True)
            for o, r in zip(pending, results):
                if isinstance(r, Exception):
                    self.state["cancel_failed"].append({"order_id": o.order_id, "symbol": o.symbol, "error": str(r)})
                else:
                    self.state["cancelled"].append({"order_id": o.order_id, "symbol": o.symbol, "side": o.side,
                                                    "remaining": o.remaining})
            await self._wait_cancels(min(CANCEL_WAIT_S, self._left(deadline_s)))
            self.state["phase"] = "CLOSING"
            await self._save()

            # 3. emergency close orders for every open position, all at once
            await self._close_open_positions(corr)
            self.state["phase"] = "AWAITING_FILLS"
            await self._save()
        self._task = asyncio.create_task(self._monitor(deadline_s, corr))
        if wait:
            await self._task
        return self.state

    def _left(self, deadline_s: float) -> float:
        return max(0.0, deadline_s - (time.monotonic() - self._t0))

    async def _close_open_positions(self, corr: str) -> int:
        """Submit a reduce-only MARKET close for every open position that has no working kill-switch order."""
        sys = self.sys
        working = {(o.strategy_id, o.symbol) for o in sys.orders.open_orders.values() if o.origin == "KILL_SWITCH"}
        now = time.monotonic()
        todo = [p for p in sys.positions.all() if p.quantity != 0 and (p.strategy_id, p.symbol) not in working
                and now - self._last_close.get((p.strategy_id, p.symbol), -1e9) >= RECLOSE_GAP_S]
        for p in todo:
            self._last_close[(p.strategy_id, p.symbol)] = now

        async def close(p):
            side = "SELL" if p.quantity > 0 else "BUY"
            entry = {"strategy_id": p.strategy_id, "symbol": p.symbol, "side": side, "quantity": abs(p.quantity),
                     "order_id": None, "status": "ERROR", "reason": None,
                     "at_s": round(time.monotonic() - self._t0, 2)}
            try:
                res = await sys.orders.submit(OrderIntent(p.strategy_id, p.symbol, side, abs(p.quantity), "MARKET", None,
                                                          "KILL_SWITCH", True, corr, None, None, "emergency flatten"))
                o = res.get("order") or {}
                entry.update(order_id=o.get("id"), status=o.get("status"), reason=o.get("reject_reason"))
            except Exception as e:
                entry["reason"] = str(e)
            self.state["close_orders"].append(entry)

        await asyncio.gather(*(close(p) for p in todo))
        return len(todo)

    async def _wait_cancels(self, timeout_s: float) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout_s
        while loop.time() < end:
            if not any(o.origin != "KILL_SWITCH" for o in self.sys.orders.open_orders.values()):
                return
            await asyncio.sleep(0.1)

    async def _monitor(self, deadline_s: float, corr: str) -> None:
        while self._left(deadline_s) > 0:
            if all(p.quantity == 0 for p in self.sys.positions.all()):
                break
            # re-close anything still open without a working close order (partial / rejected / failed close)
            if await self._close_open_positions(corr):
                await self._save()
            await asyncio.sleep(0.25)
        elapsed = round(time.monotonic() - self._t0, 2)
        # refresh close order statuses from the order engine's view
        open_ids = set(self.sys.orders.open_orders)
        for c in self.state["close_orders"]:
            if c.get("order_id") and c["status"] not in ("REJECTED", "ERROR"):
                c["status"] = "OPEN" if c["order_id"] in open_ids else "DONE"
        unresolved = [{"strategy_id": p.strategy_id, "symbol": p.symbol, "quantity": p.quantity,
                       "reason": self._why_unresolved(p.strategy_id, p.symbol)}
                      for p in self.sys.positions.all() if p.quantity != 0]
        self.state["unresolved"] = unresolved
        self.state["phase"] = "COMPLETED_WITH_ERRORS" if unresolved or self.state["cancel_failed"] else "COMPLETED"
        self.state["completed_at"] = utcnow().isoformat()
        self.state["elapsed_s"] = elapsed
        self.state["within_deadline"] = not unresolved and not self.state["cancel_failed"] and elapsed <= deadline_s
        if unresolved:
            await self.sys.audit.record("UNRESOLVED_POSITION", f"kill switch could not flatten {len(unresolved)} position(s) "
                                        f"within {deadline_s:g}s: "
                                        + "; ".join(f"{u['strategy_id']} {u['symbol']} {u['quantity']:+}" for u in unresolved),
                                        source="KillSwitch", severity="CRITICAL", correlation_id=corr)
        await self.sys.audit.record("KILL_SWITCH_COMPLETED", f"kill switch {self.state['phase']} in {elapsed:.2f}s: "
                                    f"{len(self.state['cancelled'])} cancelled, {len(self.state['close_orders'])} close orders, "
                                    f"{len(unresolved)} unresolved", source="KillSwitch", severity="WARN", correlation_id=corr)
        await self._save()
        await self.sys.pnl.publish()

    def _why_unresolved(self, sid: str, sym: str) -> str:
        c = next((c for c in self.state["close_orders"] if c["strategy_id"] == sid and c["symbol"] == sym), None)
        if c is None:
            return "position appeared after close orders were sent"
        if c["status"] in ("REJECTED", "ERROR"):
            return f"close order failed: {c.get('reason')}"
        return "close order not (fully) filled - market data/broker not executing"

    async def retry_close(self, actor: str) -> dict[str, Any]:
        """Re-attempt flattening unresolved positions while staying in SAFE MODE."""
        if not self.state.get("active"):
            raise ValueError("kill switch is not active")
        self.state["phase"] = "COMPLETED_WITH_ERRORS"
        return await self.activate(actor, "retry emergency close of unresolved positions")

    async def reset(self, actor: str) -> dict[str, Any]:
        sys = self.sys
        if sys.controller.status != SAFE_MODE:
            raise ValueError("system is not in SAFE MODE")
        rec = await sys.reconciler.run("KILL_SWITCH_RESET")
        if rec.get("status") not in ("MATCHED", "MISMATCH_RESOLVED"):
            raise ValueError(f"cannot leave SAFE MODE: reconciliation {rec.get('status')} - {rec.get('summary')}")
        open_pos = [p for p in sys.positions.all() if p.quantity != 0]
        self.state = {**self.state, "active": False, "phase": "RESET", "reset_by": actor, "reset_at": utcnow().isoformat(),
                      "positions_at_reset": [{"strategy_id": p.strategy_id, "symbol": p.symbol, "quantity": p.quantity}
                                             for p in open_pos]}
        await sys.controller.set_status(PAUSED, f"SAFE MODE cleared by {actor} after successful reconciliation", actor=actor)
        await sys.audit.record("KILL_SWITCH_RESET", f"SAFE MODE cleared by {actor}; trading PAUSED pending operator resume",
                               source="KillSwitch", severity="WARN", actor=actor)
        await self._save()
        return self.state

    def snapshot(self) -> dict[str, Any]:
        return {**self.state, "safe_mode": self.sys.controller.status == SAFE_MODE}

