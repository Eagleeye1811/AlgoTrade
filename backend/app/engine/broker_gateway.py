"""Wraps the BrokerAdapter: measures latency, tracks consecutive failures (circuit state), heartbeats."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar

from app.brokers.base import BrokerAdapter, BrokerRejected, BrokerTimeout, BrokerUnavailable
from app.core.events import Event
from app.core.health import FAILED, HEALTHY, WARNING

if TYPE_CHECKING:
    from app.engine.system import TradingSystem

log = logging.getLogger("algotrade.broker_gateway")
T = TypeVar("T")


class BrokerGateway:
    def __init__(self, system: "TradingSystem", adapter: BrokerAdapter):
        self.sys = system
        self.adapter = adapter
        self.latency_ms: float | None = None  # EWMA of round-trip latency
        self.last_sample_ms: float | None = None
        self.last_sample_at = 0.0
        self._last_sample_started = 0.0
        self.samples: deque[float] = deque(maxlen=50)
        self.consecutive_failures = 0
        self.connected = False
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None

    @property
    def available(self) -> bool:
        return self.connected and self.consecutive_failures < 3

    def current_latency(self) -> float | None:
        """Latest measured round-trip if recent, else the EWMA (used by the latency guard)."""
        if self.last_sample_ms is not None and time.monotonic() - self.last_sample_at < 10:
            return self.last_sample_ms
        return self.latency_ms

    def _record_latency(self, ms: float, started: float) -> None:
        # the most recently *initiated* call is the freshest measurement (a slow call that began
        # earlier must not overwrite a newer, faster one)
        if started >= self._last_sample_started:
            self._last_sample_started = started
            self.last_sample_ms, self.last_sample_at = ms, time.monotonic()
        self.samples.append(ms)
        self.latency_ms = ms if self.latency_ms is None else 0.4 * ms + 0.6 * self.latency_ms

    async def call(self, op: str, fn: Callable[[], Awaitable[T]]) -> T:
        t = time.perf_counter()
        try:
            res = await fn()
        except BrokerRejected:
            self._record_latency((time.perf_counter() - t) * 1000, t)
            self._ok()
            raise
        except (BrokerTimeout, BrokerUnavailable, asyncio.TimeoutError) as e:
            self._record_latency((time.perf_counter() - t) * 1000, t)
            await self._fail(op, str(e) or type(e).__name__)
            raise
        self._record_latency((time.perf_counter() - t) * 1000, t)
        self._ok()
        return res

    def _ok(self) -> None:
        was_down = not self.available
        self.consecutive_failures = 0
        self.connected = True
        self.last_error = None
        if was_down:
            asyncio.get_running_loop().create_task(self._announce_recovered())

    async def _announce_recovered(self) -> None:
        await self.sys.audit.record("BROKER_RECOVERED", "broker connectivity restored", source="BrokerAdapter")
        await self.sys.bus.publish(Event("broker.status", "BrokerAdapter", payload=self.info()))
        await self.sys.reconciler.request("BROKER_RECOVERED")

    async def _fail(self, op: str, msg: str) -> None:
        self.consecutive_failures += 1
        self.last_error = f"{op}: {msg}"
        self.sys.health.error("broker", self.last_error, FAILED if not self.available else WARNING)
        if self.consecutive_failures == 3:
            await self.sys.audit.record("BROKER_DOWN", f"broker marked unavailable after 3 failures ({self.last_error})",
                                        source="BrokerAdapter", severity="ERROR")
            await self.sys.bus.publish(Event("broker.status", "BrokerAdapter", payload=self.info()))
            await self.sys.on_component_failure("broker", self.last_error)

    async def heartbeat_loop(self) -> None:
        while True:
            try:
                await self.call("ping", self.adapter.ping)
                lat = self.current_latency() or 0
                status = HEALTHY if lat < float(self.sys.limits.g("latency_warn_ms")) else WARNING
                self.sys.health.beat("broker", status, f"{self.adapter.name} connected ({lat:.0f} ms)", lat)
            except Exception:
                pass
            await asyncio.sleep(2.0)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.heartbeat_loop(), name="broker-heartbeat")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def info(self) -> dict[str, Any]:
        return {"name": self.adapter.name, "paper": self.adapter.is_paper, "connected": self.connected,
                "available": self.available, "latency_ms": round(self.latency_ms, 1) if self.latency_ms else None,
                "current_latency_ms": round(self.current_latency(), 1) if self.current_latency() else None,
                "consecutive_failures": self.consecutive_failures, "last_error": self.last_error}
