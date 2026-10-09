"""WebSocket fan-out. Events flow: EventBus -> Redis pub/sub -> WSManager -> browsers.
If Redis is down, events are delivered directly in-process (and Redis health shows FAILED)."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict

from fastapi import WebSocket

from app.core.events import CHANNEL, Event
from app.core.redis_client import RedisClient

log = logging.getLogger("algotrade.ws")
# Candle updates are only sent for the symbols a client subscribed to (its open chart). Ticks go to every
# client so watchlists stay live; they are already throttled to <= 4/s per symbol by the market service.
SYMBOL_FILTERED_EVENTS = ("candle.update",)


class _Client:
    def __init__(self, ws: WebSocket, user: str):
        self.ws = ws
        self.user = user
        self.symbols: set[str] | None = None  # None = all symbols
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=2000)


class WSManager:
    def __init__(self, redis: RedisClient):
        self.redis = redis
        self.clients: set[_Client] = set()
        self._sub_task: asyncio.Task | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.sent = 0
        self.via_redis = False

    async def fanout(self, event: Event) -> None:
        data = event.to_json()
        if self.redis.available and await self.redis.publish(CHANNEL, data):
            self.via_redis = True
            return
        self.via_redis = False
        await self.broadcast(data, event.event_type, event.symbol, event.event_id)

    async def start(self) -> None:
        if self.redis.r is None:
            return
        self._sub_task = asyncio.create_task(self._subscribe(), name="ws-redis-sub")

    async def stop(self) -> None:
        if self._sub_task:
            self._sub_task.cancel()

    async def _subscribe(self) -> None:
        while True:
            try:
                ps = self.redis.pubsub()
                await ps.subscribe(self.redis.ch(CHANNEL))
                async for msg in ps.listen():
                    if msg.get("type") != "message":
                        continue
                    data = msg["data"]
                    try:
                        head = json.loads(data)
                    except ValueError:
                        continue
                    await self.broadcast(data, head.get("event_type", ""), head.get("symbol"), head.get("event_id"))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("redis subscription lost: %s - retrying", e)
                await asyncio.sleep(2)

    async def broadcast(self, data: str, event_type: str, symbol: str | None, event_id: str | None) -> None:
        if event_id:
            if event_id in self._seen:
                return  # duplicate delivery prevention
            self._seen[event_id] = None
            if len(self._seen) > 20000:
                self._seen.popitem(last=False)
        for c in list(self.clients):
            if event_type in SYMBOL_FILTERED_EVENTS and c.symbols is not None and symbol not in c.symbols:
                continue
            try:
                c.queue.put_nowait(data)
            except asyncio.QueueFull:
                pass  # slow client: drop (it re-syncs via REST)

    async def serve(self, ws: WebSocket, user: str) -> None:
        c = _Client(ws, user)
        self.clients.add(c)

        async def writer():
            while True:
                msg = await c.queue.get()
                await ws.send_text(msg)
                self.sent += 1

        wt = asyncio.create_task(writer())
        try:
            c.queue.put_nowait(json.dumps({"event_type": "ws.connected", "payload": {"user": user}}))
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if "symbols" in msg:
                    syms = msg["symbols"]
                    c.symbols = set(syms) if isinstance(syms, list) and syms else None
                elif msg.get("type") == "ping":
                    c.queue.put_nowait(json.dumps({"event_type": "ws.pong"}))
        except Exception:
            pass
        finally:
            wt.cancel()
            self.clients.discard(c)
