"""Redis access: market-data cache, pub/sub, rate limiting, locks, real-time risk cache.

Redis is never the financial source of truth. If Redis is unreachable the platform
degrades to process-local equivalents and reports Redis as FAILED in System Health.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import redis.asyncio as aioredis

from app.core.events import json_default

log = logging.getLogger("algotrade.redis")


class RedisClient:
    def __init__(self, url: str, namespace: str = "algotrade"):
        self.url = url
        self.ns = namespace
        self.r: aioredis.Redis | None = None
        self.available = False
        self.last_error: str | None = None
        self._local_kv: dict[str, Any] = {}
        self._local_windows: dict[str, list[float]] = {}
        self._local_locks: dict[str, asyncio.Lock] = {}

    def k(self, key: str) -> str:
        return f"{self.ns}:{key}"

    def ch(self, channel: str) -> str:
        # pub/sub channels are server-wide (not per logical DB), so include the DB index to keep
        # e.g. the test suite on /15 from broadcasting into a live instance on /0
        db = self.r.connection_pool.connection_kwargs.get("db", 0) if self.r is not None else 0
        return f"{self.ns}:{db}:{channel}"

    async def connect(self) -> None:
        try:
            self.r = aioredis.from_url(self.url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
            await self.r.ping()
            self.available = True
            self.last_error = None
        except Exception as e:  # pragma: no cover - environment dependent
            self.available = False
            self.last_error = str(e)
            log.warning("Redis unavailable (%s) - using process-local fallback", e)

    async def close(self) -> None:
        if self.r is not None:
            await self.r.aclose()

    async def ping(self) -> float:
        """Returns latency in ms; raises if unavailable."""
        if self.r is None:
            raise RuntimeError("redis not configured")
        t = time.perf_counter()
        try:
            await self.r.ping()
            self.available = True
            self.last_error = None
        except Exception as e:
            self.available = False
            self.last_error = str(e)
            raise
        return (time.perf_counter() - t) * 1000

    async def _try(self, coro_fn, fallback):
        if self.available and self.r is not None:
            try:
                return await coro_fn()
            except Exception as e:
                self.available = False
                self.last_error = str(e)
                log.warning("redis op failed, falling back: %s", e)
        return fallback()

    # ---------------- cache
    async def set_json(self, key: str, value: Any, ttl_s: int | None = None) -> None:
        data = json.dumps(value, default=json_default)
        self._local_kv[key] = data

        async def op():
            await self.r.set(self.k(key), data, ex=ttl_s)

        await self._try(op, lambda: None)

    async def get_json(self, key: str) -> Any:
        async def op():
            v = await self.r.get(self.k(key))
            return json.loads(v) if v else None

        def fb():
            v = self._local_kv.get(key)
            return json.loads(v) if v else None

        return await self._try(op, fb)

    # ---------------- pub/sub
    async def publish(self, channel: str, message: str) -> bool:
        async def op():
            await self.r.publish(self.ch(channel), message)
            return True

        return await self._try(op, lambda: False)

    def pubsub(self):
        return self.r.pubsub() if self.r is not None else None

    # ---------------- rate limiting (sliding window)
    async def count_in_window(self, key: str, window_s: float, add: bool = False) -> int:
        now = time.time()

        async def op():
            rk = self.k(f"rl:{key}")
            pipe = self.r.pipeline()
            pipe.zremrangebyscore(rk, 0, now - window_s)
            if add:
                pipe.zadd(rk, {f"{now}:{uuid.uuid4().hex[:6]}": now})
                pipe.expire(rk, int(window_s) + 5)
            pipe.zcard(rk)
            res = await pipe.execute()
            return int(res[-1])

        def fb():
            w = [t for t in self._local_windows.get(key, []) if t > now - window_s]
            if add:
                w.append(now)
            self._local_windows[key] = w
            return len(w)

        return await self._try(op, fb)

    async def reset_window(self, prefix: str) -> None:
        self._local_windows = {k: v for k, v in self._local_windows.items() if not k.startswith(prefix)}

        async def op():
            async for key in self.r.scan_iter(self.k(f"rl:{prefix}*")):
                await self.r.delete(key)

        await self._try(op, lambda: None)

    # ---------------- locks
    @asynccontextmanager
    async def lock(self, name: str, timeout_s: float = 30.0) -> AsyncIterator[None]:
        local = self._local_locks.setdefault(name, asyncio.Lock())
        async with local:  # serialises within this process regardless of Redis
            rlock = None
            if self.available and self.r is not None:
                try:
                    rlock = self.r.lock(self.k(f"lock:{name}"), timeout=timeout_s, blocking_timeout=timeout_s)
                    await rlock.acquire()
                except Exception as e:
                    log.warning("redis lock %s unavailable: %s", name, e)
                    rlock = None
            try:
                yield
            finally:
                if rlock is not None:
                    try:
                        await rlock.release()
                    except Exception:
                        pass
