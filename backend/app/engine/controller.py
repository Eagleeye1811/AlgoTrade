"""Global trading status + order-blocking conditions (fail-closed)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.events import Event

if TYPE_CHECKING:
    from app.engine.system import TradingSystem

RUNNING, PAUSED, SAFE_MODE = "RUNNING", "PAUSED", "SAFE_MODE"


class TradingController:
    def __init__(self, system: "TradingSystem"):
        self.sys = system
        self.status = PAUSED
        self.status_reason = "system starting"
        # key -> human readable reason. Any entry blocks NEW orders (risk-reducing kill-switch orders excepted).
        self.blocks: dict[str, str] = {}

    async def load(self) -> None:
        st = await self.sys.state.get("trading_status")
        if st and st.get("status") == SAFE_MODE:
            self.status, self.status_reason = SAFE_MODE, st.get("reason") or "SAFE MODE persisted from previous run"
        else:
            # After any restart we come up PAUSED: an operator must explicitly resume trading.
            self.status = PAUSED
            self.status_reason = "restarted - trading must be resumed by an operator"
        await self._persist()

    async def _persist(self) -> None:
        await self.sys.state.set("trading_status", {"status": self.status, "reason": self.status_reason})

    async def set_status(self, status: str, reason: str, actor: str | None = None) -> None:
        prev = self.status
        self.status, self.status_reason = status, reason
        await self._persist()
        await self.sys.audit.record("TRADING_STATUS_CHANGED", f"trading {prev} -> {status}: {reason}",
                                    source="TradingController", actor=actor, severity="WARN" if status == SAFE_MODE else "INFO",
                                    payload={"from": prev, "to": status, "reason": reason})
        await self.publish()

    async def block(self, key: str, reason: str) -> None:
        if self.blocks.get(key) == reason:
            return
        self.blocks[key] = reason
        await self.sys.audit.record("ORDERS_BLOCKED", f"new orders blocked: {reason}", source="TradingController",
                                    severity="WARN", payload={"key": key, "reason": reason})
        await self.publish()

    async def unblock(self, key: str, note: str = "") -> None:
        if key in self.blocks:
            reason = self.blocks.pop(key)
            await self.sys.audit.record("ORDERS_UNBLOCKED", f"order block cleared ({reason}) {note}".strip(),
                                        source="TradingController", payload={"key": key})
            await self.publish()

    def snapshot(self) -> dict:
        return {"status": self.status, "reason": self.status_reason, "blocks": dict(self.blocks),
                "orders_allowed": self.status == RUNNING and not self.blocks,
                "safe_mode": self.status == SAFE_MODE}

    async def publish(self) -> None:
        await self.sys.bus.publish(Event("trading.status", "TradingController", payload=self.snapshot()))
