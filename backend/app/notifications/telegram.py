"""Telegram copilot: explainable trading alerts and two-tap control from the trader's phone.

Linking: the web app issues a one-time code (Redis, 10 min); the trader opens t.me/<bot>?start=<code> and the bot
binds that private chat to the AlgoTrade user (table telegram_links). Only linked private chats are served.

Alerts (bus events -> the owner of the strategy; ADMIN users also get account-wide alerts):
  trades       one message per order when it completes (partial fills are aggregated), with origin
               (signal / stop-loss / target / square-off / kill switch / halt-and-flatten), position and P&L
  rejections   Risk Firewall / broker rejections with the rule and reason (one per strategy+rule per minute)
  risk         circuit-breaker halt + halt-and-flatten result, 80% of the strategy's daily loss limit used
  system       kill switch activated / completed (everyone); orders blocked, SAFE MODE, component failures (ADMIN)
  summary      end-of-day P&L summary (weekdays, settings.telegram_summary_at IST)

Commands: /status /pnl /positions /strategies (Pause/Resume buttons) /pause /resume /summary /kill (ADMIN, confirm
button valid 30 s) /mute /unmute /unlink /help. Authorization is the web app's: a trader manages only their own
strategies, an ADMIN all of them. Every action is audited with actor "telegram:<email>".

Delivery: one outgoing queue; at most one message per chat per second, retries on HTTP 429 (retry_after).
The bot token is never logged (errors are scrubbed).
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, select

from app.core.events import Event
from app.core.health import FAILED, HEALTHY, WARNING
from app.database.models import TelegramLink, User

if TYPE_CHECKING:
    from app.engine.system import TradingSystem

log = logging.getLogger("algotrade.telegram")
IST = ZoneInfo("Asia/Kolkata")
TOKEN_RE = re.compile(r"\d{5,15}:[A-Za-z0-9_-]{30,}")  # <bot id>:<secret> as issued by @BotFather
LINK_TTL_S = 600
KILL_CONFIRM_S = 30
REJECT_THROTTLE_S = 60
PER_CHAT_GAP_S = 1.0
CATEGORIES = ("trades", "rejections", "risk", "system", "summary")
TERMINAL = ("FILLED", "CANCELLED", "REJECTED", "EXPIRED")
ORIGIN_LABEL = {
    "STRATEGY": "strategy signal", "STOP_LOSS": "🛑 stop-loss exit", "TAKE_PROFIT": "🎯 target exit",
    "SQUARE_OFF": "⏰ intraday square-off", "KILL_SWITCH": "🚨 kill switch close",
    "CIRCUIT_BREAKER": "🧯 halt-and-flatten close", "FAULT_LAB": "🧪 Fault Lab test order", "DEMO": "demo order",
    "RUNAWAY": "runaway (simulated malfunction)",
}


class TelegramError(Exception):
    def __init__(self, message: str, code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.code, self.retry_after = code, retry_after


class TelegramAPI:
    """Minimal Bot API client (HTTPS, JSON)."""

    def __init__(self, token: str, base: str = "https://api.telegram.org"):
        self._token = token
        self._url = f"{base.rstrip('/')}/bot{token}/"
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0))

    def _scrub(self, s: str) -> str:
        return s.replace(self._token, "<token>") if self._token else s

    async def call(self, method: str, **params: Any) -> Any:
        try:
            r = await self._http.post(self._url + method, json={k: v for k, v in params.items() if v is not None})
            d = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(self._scrub(f"{type(e).__name__}: {e}")) from None
        if not d.get("ok"):
            raise TelegramError(self._scrub(str(d.get("description"))), d.get("error_code"),
                                (d.get("parameters") or {}).get("retry_after"))
        return d.get("result")

    async def close(self) -> None:
        await self._http.aclose()


@dataclass
class Link:
    user_id: int
    chat_id: int
    email: str
    role: str
    muted: bool = False
    prefs: dict = field(default_factory=dict)

    def wants(self, category: str) -> bool:
        return not self.muted and self.prefs.get(category, True)

    @property
    def actor(self) -> str:
        return f"telegram:{self.email}"


def esc(v: Any) -> str:
    return html.escape(str(v), quote=False)


def inr(v: Any, signed: bool = False) -> str:
    d = Decimal(str(v or 0))
    s = f"₹{abs(d):,.2f}"
    return (("+" if d > 0 else "-" if d < 0 else "") + s) if signed else ("-" + s if d < 0 else s)


def qty(v: Any) -> str:
    d = Decimal(str(v or 0))
    return f"{d:+,.0f}" if d else "0"


class TelegramService:
    def __init__(self, system: "TradingSystem", api: TelegramAPI | None = None):
        self.sys = system
        s = system.settings
        self.api = api or (TelegramAPI(s.telegram_bot_token, s.telegram_api_base) if s.telegram_bot_token else None)
        self.enabled = self.api is not None
        self.bot_username: str | None = None
        self.links: dict[int, Link] = {}           # chat_id -> link
        self.by_user: dict[int, int] = {}          # user_id -> chat_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sent = 0
        self.failed = 0
        self.last_error: str | None = None
        self._tasks: list[asyncio.Task] = []
        self._offset: int | None = None
        self._last_sent: dict[int, float] = {}
        self._fills: dict[str, int] = {}           # order id -> fills seen
        self._reject_seen: dict[tuple, float] = {}
        self._kill_nonces: dict[str, tuple[int, float]] = {}
        self._kill_phase: str | None = None
        self._blocks: dict[str, str] = {}
        self._ctl_status: str | None = None
        self._loss_warned: dict[str, Any] = {}
        self._summary_day = None
        self._subscribed = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if not self.enabled:
            self.sys.health.beat("telegram", HEALTHY, "disabled - set ALGOTRADE_TELEGRAM_BOT_TOKEN to enable")
            return
        await self.load_links()
        self.attach()
        token = self.sys.settings.telegram_bot_token
        if token and not TOKEN_RE.fullmatch(token.strip()):
            self.last_error = ("the bot token is malformed: it must look like <bot id>:<secret>, e.g. 7123456789:AAH... - "
                               "copy the whole token from @BotFather into ALGOTRADE_TELEGRAM_BOT_TOKEN")
            self.sys.health.error("telegram", self.last_error, WARNING)
            return
        try:
            me = await self.api.call("getMe")
            self.bot_username = me.get("username")
            self.sys.health.beat("telegram", HEALTHY, f"@{self.bot_username} - {len(self.links)} linked chat(s)")
        except TelegramError as e:
            self.last_error = str(e)
            self.sys.health.error("telegram", f"bot unreachable: {e}", WARNING)
        self._tasks = [asyncio.create_task(self._poll_loop(), name="telegram-poll"),
                       asyncio.create_task(self._send_loop(), name="telegram-send"),
                       asyncio.create_task(self._periodic_loop(), name="telegram-periodic")]

    def attach(self) -> None:
        """Subscribe to the platform events that become alerts."""
        if not self._subscribed:
            for prefix in ("order.update", "fill.received", "strategy.status", "kill_switch.update", "trading.status",
                           "system.alert", "audit.event"):
                self.sys.bus.subscribe(prefix, self.on_event)
            self._subscribed = True

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        if self.api is not None:
            await self.api.close()

    async def load_links(self) -> None:
        async with self.sys.db.session() as s:
            rows = (await s.execute(select(TelegramLink, User).join(User, User.id == TelegramLink.user_id))).all()
        self.links, self.by_user = {}, {}
        for tl, u in rows:
            if u.is_active:
                self._cache(Link(u.id, tl.chat_id, u.email, u.role, tl.muted, dict(tl.prefs or {})))

    def _cache(self, link: Link) -> None:
        old = self.by_user.get(link.user_id)
        if old is not None and old != link.chat_id:
            self.links.pop(old, None)
        self.links[link.chat_id] = link
        self.by_user[link.user_id] = link.chat_id

    def info(self, user_id: int | None = None) -> dict[str, Any]:
        link = self.links.get(self.by_user.get(user_id, -1)) if user_id is not None else None
        return {"configured": self.enabled, "bot_username": self.bot_username, "linked": link is not None,
                "muted": link.muted if link else False,
                "prefs": {c: (link.prefs.get(c, True) if link else True) for c in CATEGORIES},
                "linked_chats": len(self.links), "sent": self.sent, "failed": self.failed, "last_error": self.last_error}

    # ------------------------------------------------------------------ linking (web app side)
    async def create_link_code(self, user: User) -> dict[str, Any]:
        if not self.enabled:
            raise ValueError("Telegram is not configured on this server (ALGOTRADE_TELEGRAM_BOT_TOKEN)")
        if not self.bot_username:
            if self.last_error and ("malformed" in self.last_error or "Not Found" in self.last_error
                                    or "Unauthorized" in self.last_error):
                raise ValueError("Telegram rejected the bot token" + ("" if "malformed" in self.last_error else
                                 " (invalid or revoked token)") + f": {self.last_error}. Fix ALGOTRADE_TELEGRAM_BOT_TOKEN "
                                 "in backend/.env and restart the backend.")
            raise ValueError("the Telegram bot is not reachable right now - try again in a moment"
                             + (f" ({self.last_error})" if self.last_error else ""))
        code = secrets.token_urlsafe(12)
        await self.sys.redis.set_json(f"tg:link:{code}", {"user_id": user.id}, ttl_s=LINK_TTL_S)
        return {"code": code, "url": f"https://t.me/{self.bot_username}?start={code}", "expires_in_s": LINK_TTL_S}

    async def unlink(self, user_id: int, actor: str) -> None:
        chat = self.by_user.pop(user_id, None)
        if chat is not None:
            self.links.pop(chat, None)
        async with self.sys.db.transaction() as s:
            await s.execute(delete(TelegramLink).where(TelegramLink.user_id == user_id))
        await self.sys.audit.record("TELEGRAM_UNLINKED", f"Telegram chat unlinked from user {user_id}", source="Telegram",
                                    actor=actor)
        if chat is not None:
            await self.send(chat, "🔌 This chat is no longer linked to AlgoTrade.")

    async def set_prefs(self, user_id: int, muted: bool | None = None, prefs: dict[str, bool] | None = None) -> None:
        link = self.links.get(self.by_user.get(user_id, -1))
        if link is None:
            raise ValueError("Telegram is not linked for this user")
        if muted is not None:
            link.muted = muted
        if prefs:
            link.prefs.update({k: bool(v) for k, v in prefs.items() if k in CATEGORIES})
        async with self.sys.db.transaction() as s:
            row = await s.get(TelegramLink, user_id)
            row.muted, row.prefs = link.muted, dict(link.prefs)

    async def _link_chat(self, code: str, chat_id: int, username: str | None) -> str:
        data = await self.sys.redis.get_json(f"tg:link:{code}")
        if not data:
            return "❌ This link has expired or was already used. Create a new one in AlgoTrade → Settings → Telegram."
        await self.sys.redis.set_json(f"tg:link:{code}", None, ttl_s=1)  # single use
        async with self.sys.db.transaction() as s:
            user = await s.get(User, int(data["user_id"]))
            if user is None or not user.is_active:
                return "❌ That AlgoTrade user no longer exists."
            await s.execute(delete(TelegramLink).where((TelegramLink.chat_id == chat_id) |
                                                       (TelegramLink.user_id == user.id)))
            s.add(TelegramLink(user_id=user.id, chat_id=chat_id, tg_username=username, muted=False, prefs={}))
            email, role, uid = user.email, user.role, user.id
        for c, l in list(self.links.items()):
            if c == chat_id or l.user_id == uid:
                self.links.pop(c, None)
        self._cache(Link(uid, chat_id, email, role))
        await self.sys.audit.record("TELEGRAM_LINKED", f"Telegram chat linked to {email} ({role})", source="Telegram",
                                    actor=f"telegram:{email}", payload={"tg_username": username})
        return (f"✅ Linked to <b>{esc(email)}</b> ({esc(role)}).\n\nYou will get trade, risk and system alerts here. "
                "Send /help for commands.")

    # ------------------------------------------------------------------ incoming updates
    async def _poll_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                updates = await self.api.call("getUpdates", offset=self._offset, timeout=25,
                                              allowed_updates=["message", "callback_query"])
                backoff = 1.0
                self.sys.health.beat("telegram", HEALTHY, f"@{self.bot_username} - {len(self.links)} linked chat(s)")
                for u in updates or []:
                    self._offset = u["update_id"] + 1
                    try:
                        await self.handle_update(u)
                    except Exception:
                        log.exception("telegram update failed")
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                self.last_error = str(e)
                self.sys.health.error("telegram", f"polling failed: {e}", WARNING)
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    async def handle_update(self, u: dict) -> None:
        if "callback_query" in u:
            return await self._on_callback(u["callback_query"])
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        text = (msg.get("text") or "").strip()
        if not text or chat.get("type") != "private":
            return
        chat_id = chat["id"]
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        if cmd == "/start":
            if arg:
                return await self.send(chat_id, await self._link_chat(arg, chat_id, (msg.get("from") or {}).get("username")))
            if chat_id not in self.links:
                return await self.send(chat_id, "👋 This is the AlgoTrade bot. Link this chat from AlgoTrade → "
                                                "Settings → Telegram → Connect.")
        link = self.links.get(chat_id)
        if link is None:
            return await self.send(chat_id, "🔒 This chat is not linked. Open AlgoTrade → Settings → Telegram → Connect.")
        handler = {"/start": self._cmd_help, "/help": self._cmd_help, "/status": self._cmd_status,
                   "/pnl": self._cmd_pnl, "/positions": self._cmd_positions, "/strategies": self._cmd_strategies,
                   "/pause": self._cmd_pause, "/resume": self._cmd_resume, "/summary": self._cmd_summary,
                   "/kill": self._cmd_kill, "/mute": self._cmd_mute, "/unmute": self._cmd_unmute,
                   "/unlink": self._cmd_unlink}.get(cmd)
        if handler is None:
            return await self.send(chat_id, "Unknown command. Send /help.")
        await handler(link, arg)

    async def _on_callback(self, cq: dict) -> None:
        chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
        link = self.links.get(chat_id)
        data = cq.get("data") or ""
        answer = "done"
        try:
            if link is None:
                answer = "this chat is not linked"
            elif data.startswith("p:") or data.startswith("r:"):
                answer = await self._set_strategy(link, data[2:], "PAUSED" if data[0] == "p" else "RUNNING")
            elif data.startswith("kill:"):
                answer = await self._confirm_kill(link, data[5:])
        finally:
            try:
                await self.api.call("answerCallbackQuery", callback_query_id=cq.get("id"), text=answer[:190])
            except TelegramError:
                pass

    # ------------------------------------------------------------------ commands
    def _mine(self, link: Link) -> list:
        rts = [rt for rt in self.sys.strategies.runtimes.values() if rt.user_id == link.user_id]
        return sorted(rts, key=lambda r: r.id)

    def _resolve(self, link: Link, arg: str):
        rts = self._mine(link)
        if arg.isdigit() and 1 <= int(arg) <= len(rts):
            return rts[int(arg) - 1]
        rt = self.sys.strategies.runtimes.get(arg)
        if rt is not None and (link.role == "ADMIN" or rt.user_id == link.user_id):
            return rt
        return None

    async def _cmd_help(self, link: Link, arg: str) -> None:
        admin = "\n/kill - KILL SWITCH: cancel everything and flatten (confirm button)" if link.role == "ADMIN" else ""
        await self.send(link.chat_id, (
            f"<b>AlgoTrade</b> · {esc(link.email)} ({esc(link.role)})\n\n"
            "/status - trading state and today's P&L\n/pnl - P&L per strategy\n/positions - open positions\n"
            "/strategies - your strategies with Pause/Resume buttons\n/pause &lt;n|id&gt; · /resume &lt;n|id&gt;\n"
            "/summary - today's summary\n/mute · /unmute - alerts off/on\n/unlink - disconnect this chat" + admin))

    async def _cmd_status(self, link: Link, arg: str) -> None:
        ctl = self.sys.controller.snapshot()
        m = self.sys.market.info()
        mine = self._pnl_of(self._mine(link))
        lines = [f"<b>Trading:</b> {esc(ctl['status'])}" + (" · orders allowed" if ctl["orders_allowed"] else " · orders BLOCKED")]
        for k, v in ctl["blocks"].items():
            lines.append(f"  ⛔ {esc(v)}")
        ah = m.get("after_hours") or {}
        src = m.get("source") if m.get("mode") == "REPLAY" else \
            "replay of the recorded 021 session (after hours)" if "replay" in ah.values() else \
            "021 sandbox stream (after hours)" if ah else "021 live feed"
        lines.append(f"<b>Market data:</b> {esc(m.get('display_status'))} · {src}")
        lines.append(f"<b>Broker:</b> {'connected' if self.sys.broker.available else 'UNAVAILABLE'}")
        lines.append(f"<b>Your P&amp;L today:</b> {inr(mine['today'], True)} · open positions: {mine['open']}")
        if link.role == "ADMIN":
            pf = (self.sys.pnl.last or self.sys.pnl.compute())["portfolio"]
            lines.append(f"<b>Account P&amp;L today:</b> {inr(pf['today_pnl'], True)} · orders today: {pf['orders_today']}")
        await self.send(link.chat_id, "\n".join(lines))

    def _pnl_of(self, rts: list) -> dict:
        st = (self.sys.pnl.last or self.sys.pnl.compute())["strategies"]
        today = sum((Decimal(str(st.get(rt.id, {}).get("today_pnl", 0))) for rt in rts), Decimal(0))
        open_ = sum(1 for rt in rts if self.sys.positions.get(rt.id, rt.symbol).quantity != 0)
        return {"today": today, "open": open_}

    async def _cmd_pnl(self, link: Link, arg: str) -> None:
        rts = self._mine(link)
        if not rts:
            return await self.send(link.chat_id, "You have no subscriptions.")
        st = (self.sys.pnl.last or self.sys.pnl.compute())["strategies"]
        lines = ["<b>P&amp;L today</b>"]
        for i, rt in enumerate(rts, 1):
            p = st.get(rt.id, {})
            lines.append(f"{i}. {esc(rt.name)} · {esc(rt.symbol)}: <b>{inr(p.get('today_pnl'), True)}</b> "
                         f"(realized {inr(p.get('realized'), True)}, unrealized {inr(p.get('unrealized'), True)})")
        lines.append(f"\nTotal: <b>{inr(self._pnl_of(rts)['today'], True)}</b>")
        await self.send(link.chat_id, "\n".join(lines))

    async def _cmd_positions(self, link: Link, arg: str) -> None:
        lines = []
        for rt in self._mine(link):
            p = self.sys.positions.get(rt.id, rt.symbol)
            if p.quantity != 0:
                lines.append(f"{'🟢 LONG' if p.quantity > 0 else '🔴 SHORT'} {qty(p.quantity)} {esc(rt.symbol)} @ "
                             f"{inr(p.avg_price)} · LTP {inr(self.sys.market.ltp(rt.symbol))} · {esc(rt.name)}")
        await self.send(link.chat_id, "<b>Open positions</b>\n" + "\n".join(lines) if lines else "No open positions.")

    async def _cmd_strategies(self, link: Link, arg: str) -> None:
        rts = self._mine(link)
        if not rts:
            return await self.send(link.chat_id, "You have no subscriptions.")
        lines, buttons = ["<b>Your strategies</b>"], []
        for i, rt in enumerate(rts, 1):
            p = self.sys.positions.get(rt.id, rt.symbol)
            lines.append(f"{i}. <b>{esc(rt.name)}</b> · {esc(rt.symbol)} · {esc(rt.status)} · position {qty(p.quantity)}"
                         f"\n    <code>{esc(rt.id)}</code>")
            if rt.status == "RUNNING":
                buttons.append([{"text": f"⏸ Pause {i}. {rt.name[:24]}", "callback_data": f"p:{rt.id}"}])
            elif rt.status in ("PAUSED", "HALTED"):
                buttons.append([{"text": f"▶️ Resume {i}. {rt.name[:24]}", "callback_data": f"r:{rt.id}"}])
        await self.send(link.chat_id, "\n".join(lines), {"inline_keyboard": buttons} if buttons else None)

    async def _cmd_pause(self, link: Link, arg: str) -> None:
        await self._strategy_cmd(link, arg, "PAUSED")

    async def _cmd_resume(self, link: Link, arg: str) -> None:
        await self._strategy_cmd(link, arg, "RUNNING")

    async def _strategy_cmd(self, link: Link, arg: str, status: str) -> None:
        rt = self._resolve(link, arg)
        if rt is None:
            return await self.send(link.chat_id, "Which strategy? Use the number from /strategies or its id, "
                                                 f"e.g. /{'pause' if status == 'PAUSED' else 'resume'} 1")
        await self.send(link.chat_id, await self._set_strategy(link, rt.id, status))

    async def _set_strategy(self, link: Link, sid: str, status: str) -> str:
        rt = self.sys.strategies.runtimes.get(sid)
        if rt is None or rt.status == "STOPPED":
            return "strategy not found"
        if link.role != "ADMIN" and rt.user_id != link.user_id:
            return "not your subscription"
        if status == "RUNNING" and self.sys.controller.status == "SAFE_MODE":
            return "system is in SAFE MODE - an ADMIN must reset the kill switch first"
        if rt.status == status:
            return f"{rt.name} is already {status}"
        await self.sys.strategies.set_status(sid, status, f"{'paused' if status == 'PAUSED' else 'resumed'} via Telegram "
                                             f"by {link.email}", actor=link.actor)
        return f"{rt.name} ({rt.symbol}) → {status}"

    async def _cmd_summary(self, link: Link, arg: str) -> None:
        await self.send(link.chat_id, self._summary_text(link))

    def _summary_text(self, link: Link) -> str:
        rts = self._mine(link)
        st = (self.sys.pnl.last or self.sys.pnl.compute())["strategies"]
        day = datetime.now(IST).strftime("%d %b %Y")
        lines = [f"📊 <b>Summary · {day}</b>"]
        best = None
        for rt in rts:
            p = st.get(rt.id, {})
            t = Decimal(str(p.get("today_pnl", 0)))
            best = rt if best is None or t > Decimal(str(st.get(best.id, {}).get("today_pnl", 0))) else best
            pos = self.sys.positions.get(rt.id, rt.symbol).quantity
            lines.append(f"• {esc(rt.name)} · {esc(rt.symbol)}: {inr(t, True)} · position {qty(pos)} · {esc(rt.status)}")
        total = self._pnl_of(rts)["today"]
        lines.append(f"\n<b>Total today: {inr(total, True)}</b>")
        if best is not None and len(rts) > 1:
            lines.append(f"Best: {esc(best.name)} ({esc(best.symbol)})")
        return "\n".join(lines) if rts else f"📊 Summary · {day}: no subscriptions."

    async def _cmd_kill(self, link: Link, arg: str) -> None:
        if link.role != "ADMIN":
            return await self.send(link.chat_id, "⛔ Only an ADMIN can activate the kill switch.")
        nonce = secrets.token_hex(6)
        self._kill_nonces = {k: v for k, v in self._kill_nonces.items() if v[1] > time.monotonic()}
        self._kill_nonces[nonce] = (link.chat_id, time.monotonic() + KILL_CONFIRM_S)
        await self.send(link.chat_id, "🚨 <b>KILL SWITCH</b>\nCancels every open order, closes every position and "
                                      f"locks the system in SAFE MODE. Confirm within {KILL_CONFIRM_S} s.",
                        {"inline_keyboard": [[{"text": "🚨 CONFIRM KILL SWITCH", "callback_data": f"kill:{nonce}"}]]})

    async def _confirm_kill(self, link: Link, nonce: str) -> str:
        entry = self._kill_nonces.pop(nonce, None)
        if link.role != "ADMIN":
            return "only an ADMIN can activate the kill switch"
        if entry is None or entry[0] != link.chat_id or entry[1] < time.monotonic():
            return "confirmation expired - send /kill again"
        await self.sys.audit.record("TELEGRAM_KILL_SWITCH", f"kill switch confirmed via Telegram by {link.email}",
                                    source="Telegram", severity="CRITICAL", actor=link.actor)
        asyncio.create_task(self.sys.kill_switch.activate(link.actor, "activated via Telegram"))
        return "kill switch activated"

    async def _cmd_mute(self, link: Link, arg: str) -> None:
        await self.set_prefs(link.user_id, muted=True)
        await self.send(link.chat_id, "🔕 Alerts muted. Commands still work. /unmute to turn them back on.")

    async def _cmd_unmute(self, link: Link, arg: str) -> None:
        await self.set_prefs(link.user_id, muted=False)
        await self.send(link.chat_id, "🔔 Alerts on.")

    async def _cmd_unlink(self, link: Link, arg: str) -> None:
        await self.unlink(link.user_id, link.actor)

    # ------------------------------------------------------------------ alerts
    def _owner_link(self, sid: str | None) -> Link | None:
        rt = self.sys.strategies.runtimes.get(sid or "")
        if rt is None or rt.user_id is None:
            return None
        return self.links.get(self.by_user.get(rt.user_id, -1))

    def _admins(self) -> list[Link]:
        return [l for l in self.links.values() if l.role == "ADMIN"]

    async def _notify(self, links: list[Link | None], category: str, text: str) -> None:
        seen = set()
        for l in links:
            if l is not None and l.chat_id not in seen and l.wants(category):
                seen.add(l.chat_id)
                await self.send(l.chat_id, text)

    def _strategy_line(self, sid: str | None) -> str:
        rt = self.sys.strategies.runtimes.get(sid or "")
        return f"{esc(rt.name)} · <code>{esc(rt.id)}</code>" if rt else esc(sid or "-")

    async def on_event(self, e: Event) -> None:
        if not self.links:
            return
        t = e.event_type
        if t == "fill.received" and e.order_id:
            self._fills[e.order_id] = self._fills.get(e.order_id, 0) + 1
        elif t == "order.update":
            await self._on_order(e)
        elif t == "strategy.status":
            if e.payload.get("status") == "HALTED":
                await self._notify([self._owner_link(e.strategy_id)] + self._admins(), "risk",
                                   f"🧯 <b>Strategy HALTED</b>\n{self._strategy_line(e.strategy_id)}\n"
                                   f"{esc(e.payload.get('reason') or '')}")
        elif t == "kill_switch.update":
            await self._on_kill(e.payload)
        elif t == "trading.status":
            await self._on_trading(e.payload)
        elif t == "system.alert":
            await self._notify(self._admins(), "system", f"⚠️ <b>Component failure</b> · {esc(e.payload.get('component'))}\n"
                                                         f"{esc(e.payload.get('detail'))}")
        elif t == "audit.event":
            kind = e.payload.get("type")
            if kind in ("STRATEGY_FLATTENED", "STRATEGY_FLATTEN_INCOMPLETE"):
                icon = "✅" if kind == "STRATEGY_FLATTENED" else "❗"
                await self._notify([self._owner_link(e.strategy_id)] + self._admins(), "risk",
                                   f"{icon} <b>Halt and flatten</b>\n{self._strategy_line(e.strategy_id)}\n"
                                   f"{esc(e.payload.get('message'))}")
            elif kind == "UNRESOLVED_POSITION":
                await self._notify(self._admins(), "system", f"❗ <b>Unresolved position</b>\n{esc(e.payload.get('message'))}")

    async def _on_order(self, e: Event) -> None:
        o = e.payload.get("order") or {}
        status = o.get("status")
        if status not in TERMINAL:
            return
        fills = self._fills.pop(o.get("id"), 0)
        owner = self._owner_link(o.get("strategy_id"))
        origin = o.get("origin") or ""
        head = f"{o.get('side')} {Decimal(str(o.get('quantity') or 0)):,.0f} {o.get('symbol')}"
        if status == "REJECTED":
            key = (o.get("strategy_id"), o.get("reject_rule"))
            now = time.monotonic()
            if now - self._reject_seen.get(key, -1e9) < REJECT_THROTTLE_S:
                return
            self._reject_seen[key] = now
            await self._notify([owner], "rejections",
                               f"⛔ <b>Order rejected</b> · {esc(head)}\n{self._strategy_line(o.get('strategy_id'))}\n"
                               f"Rule: <b>{esc(o.get('reject_rule') or '-')}</b>\n{esc(o.get('reject_reason') or '')}\n"
                               f"<i>{esc(ORIGIN_LABEL.get(origin, origin))}</i>")
            return
        filled = Decimal(str(o.get("filled_quantity") or 0))
        if filled <= 0:
            return
        rt = self.sys.strategies.runtimes.get(o.get("strategy_id") or "")
        pos = self.sys.positions.get(rt.id, rt.symbol).quantity if rt else None
        pnl = ((self.sys.pnl.last or self.sys.pnl.compute())["strategies"].get(rt.id, {}) if rt else {})
        part = "" if status == "FILLED" else f" (partially: {filled:,.0f} of {Decimal(str(o.get('quantity'))):,.0f}, then {status.lower()})"
        icon = {"STOP_LOSS": "🛑", "TAKE_PROFIT": "🎯", "KILL_SWITCH": "🚨", "CIRCUIT_BREAKER": "🧯"}.get(origin, "✅")
        await self._notify([owner], "trades",
                           f"{icon} <b>{esc(o.get('side'))} {filled:,.0f} {esc(o.get('symbol'))}</b> filled @ "
                           f"<b>{inr(o.get('avg_fill_price'))}</b> avg" + (f" ({fills} fills)" if fills > 1 else "") + part +
                           f"\n{self._strategy_line(o.get('strategy_id'))}\n<i>{esc(ORIGIN_LABEL.get(origin, origin))}</i>"
                           + (f"\nPosition now {qty(pos)} · P&amp;L today {inr(pnl.get('today_pnl'), True)}" if rt else ""))

    async def _on_kill(self, st: dict) -> None:
        phase = st.get("phase")
        if not st.get("active") and phase not in ("COMPLETED", "COMPLETED_WITH_ERRORS"):
            self._kill_phase = phase
            return
        if phase == self._kill_phase:
            return
        prev, self._kill_phase = self._kill_phase, phase
        everyone = list(self.links.values())
        if phase == "CANCELLING" and prev != "CANCELLING":
            await self._notify(everyone, "system", f"🚨 <b>KILL SWITCH ACTIVATED</b> by {esc(st.get('by'))}\n"
                                                   "Cancelling all orders and closing all positions (10 s deadline).")
        elif phase in ("COMPLETED", "COMPLETED_WITH_ERRORS"):
            un = st.get("unresolved") or []
            ok = phase == "COMPLETED" and st.get("within_deadline")
            await self._notify(everyone, "system",
                               f"{'✅' if ok else '❗'} <b>Kill switch {esc(phase.replace('_', ' ').lower())}</b> in "
                               f"{st.get('elapsed_s')} s · {len(st.get('cancelled') or [])} order(s) cancelled, "
                               f"{len(st.get('close_orders') or [])} close order(s)"
                               + (f"\nUnresolved: " + "; ".join(f"{esc(u['strategy_id'])} {esc(u['symbol'])} {qty(u['quantity'])}"
                                                               for u in un) if un else "\nAll positions flat. System in SAFE MODE."))

    async def _on_trading(self, snap: dict) -> None:
        admins = self._admins()
        blocks = snap.get("blocks") or {}
        for k, v in blocks.items():
            if k not in self._blocks:
                await self._notify(admins, "system", f"⛔ <b>New orders blocked</b>\n{esc(v)}")
        if self._blocks and not blocks:
            await self._notify(admins, "system", "✅ Order blocks cleared - orders allowed again.")
        self._blocks = dict(blocks)
        status = snap.get("status")
        if self._ctl_status is not None and status != self._ctl_status:
            await self._notify(admins, "system", f"ℹ️ Trading {esc(self._ctl_status)} → <b>{esc(status)}</b>"
                                                 + (f"\n{esc(snap.get('reason'))}" if snap.get("reason") else ""))
        self._ctl_status = status

    # ------------------------------------------------------------------ periodic: loss warnings, daily summary
    async def _periodic_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self.periodic(datetime.now(timezone.utc))
            except Exception:
                log.exception("telegram periodic check failed")

    async def periodic(self, now: datetime) -> None:
        if not self.links:
            return
        ist = now.astimezone(IST)
        st = (self.sys.pnl.last or self.sys.pnl.compute())["strategies"]
        for rt in list(self.sys.strategies.runtimes.values()):
            owner = self._owner_link(rt.id)
            if owner is None:
                continue
            limit = self.sys.limits.daily_loss_limit(rt.id, rt.allocation)
            today = Decimal(str(st.get(rt.id, {}).get("today_pnl", 0)))
            if limit and today <= -Decimal("0.8") * limit and self._loss_warned.get(rt.id) != ist.date():
                self._loss_warned[rt.id] = ist.date()
                await self._notify([owner], "risk", f"⚠️ <b>Daily loss at {abs(today) / limit:.0%} of the limit</b>\n"
                                                    f"{self._strategy_line(rt.id)}\nP&amp;L today {inr(today, True)} · "
                                                    f"limit {inr(limit)}. New entries stop at 100%.")
        hh, mm = (int(x) for x in self.sys.settings.telegram_summary_at.split(":"))
        if ist.weekday() < 5 and ist.time() >= dtime(hh, mm) and self._summary_day != ist.date():
            self._summary_day = ist.date()
            for l in list(self.links.values()):
                if l.wants("summary"):
                    await self.send(l.chat_id, self._summary_text(l))

    # ------------------------------------------------------------------ outgoing
    async def send(self, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
        if self.enabled:
            await self.queue.put((chat_id, text, reply_markup))

    async def drain(self) -> None:
        """Deliver everything queued (tests; the send loop does this continuously)."""
        while not self.queue.empty():
            await self._deliver(*self.queue.get_nowait())
            self.queue.task_done()

    async def _send_loop(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                wait = PER_CHAT_GAP_S - (time.monotonic() - self._last_sent.get(item[0], 0))
                if wait > 0:
                    await asyncio.sleep(wait)
                await self._deliver(*item)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram send failed")
            finally:
                self.queue.task_done()

    async def _deliver(self, chat_id: int, text: str, reply_markup: dict | None) -> None:
        for attempt in range(3):
            try:
                await self.api.call("sendMessage", chat_id=chat_id, text=text[:4000], parse_mode="HTML",
                                    disable_web_page_preview=True, reply_markup=reply_markup)
                self.sent += 1
                self._last_sent[chat_id] = time.monotonic()
                return
            except TelegramError as e:
                self.last_error = str(e)
                if e.code == 429 and e.retry_after:
                    await asyncio.sleep(min(float(e.retry_after), 30))
                    continue
                if e.code in (400, 403):  # bad request / bot blocked by the user: retrying will not help
                    break
                await asyncio.sleep(1 + attempt)
        self.failed += 1
        log.warning("telegram message to %s not delivered: %s", chat_id, self.last_error)
