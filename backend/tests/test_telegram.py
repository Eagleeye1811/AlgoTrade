"""Telegram copilot: linking, explainable alerts, permission-checked commands, kill switch confirmation."""
from __future__ import annotations

import asyncio
from decimal import Decimal

from app.core.security import hash_password
from app.database.models import User
from app.notifications.telegram import TelegramService
from tests.conftest import wait_for

D = Decimal


class FakeTelegram:
    """Records Bot API calls instead of reaching api.telegram.org."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, **params):
        self.calls.append((method, params))
        return {"username": "algotest_bot"} if method == "getMe" else True

    async def close(self):
        pass

    def texts(self, chat_id: int | None = None) -> list[str]:
        return [p["text"] for m, p in self.calls if m == "sendMessage" and (chat_id is None or p["chat_id"] == chat_id)]

    def answers(self) -> list[str]:
        return [p.get("text") for m, p in self.calls if m == "answerCallbackQuery"]


async def make_user(sys_, email: str, role: str) -> User:
    async with sys_.db.transaction() as s:
        u = User(email=email, name=email.split("@")[0], password_hash=hash_password("x" * 8), role=role)
        s.add(u)
        await s.flush()
        return u


def msg(chat_id: int, text: str, chat_type: str = "private") -> dict:
    return {"update_id": 1, "message": {"chat": {"id": chat_id, "type": chat_type}, "from": {"username": "t"}, "text": text}}


def cb(chat_id: int, data: str) -> dict:
    return {"update_id": 2, "callback_query": {"id": "q", "data": data, "message": {"chat": {"id": chat_id}}}}


async def setup(sys_):
    api = FakeTelegram()
    svc = TelegramService(sys_, api=api)
    svc.bot_username = "algotest_bot"
    svc.attach()
    sys_.telegram = svc
    return svc, api


async def link(svc, user, chat_id: int):
    code = (await svc.create_link_code(user))["code"]
    await svc.handle_update(msg(chat_id, f"/start {code}"))
    await svc.drain()
    return code


async def test_linking_is_one_time_and_private_only(trading):
    svc, api = await setup(trading)
    trader = await make_user(trading, "trader@x.com", "TRADER")
    out = await svc.create_link_code(trader)
    assert out["url"] == f"https://t.me/algotest_bot?start={out['code']}"
    code = await link(svc, trader, 111)
    assert "Linked to <b>trader@x.com</b>" in api.texts(111)[-1]
    assert svc.links[111].user_id == trader.id
    # the code is single use
    await svc.handle_update(msg(222, f"/start {code}"))
    await svc.drain()
    assert "expired or was already used" in api.texts(222)[-1] and 222 not in svc.links
    # unlinked chats get nothing but instructions; group chats are ignored
    await svc.handle_update(msg(333, "/status"))
    await svc.handle_update(msg(111, "/status", chat_type="group"))
    await svc.drain()
    assert "not linked" in api.texts(333)[-1]
    assert len(api.texts(111)) == 1
    # the link survives a restart (loaded from PostgreSQL)
    await svc.load_links()
    assert svc.links[111].email == "trader@x.com"


async def test_fill_alert_is_one_message_per_order_with_strategy_and_position(trading):
    sys_ = trading
    svc, api = await setup(sys_)
    trader = await make_user(sys_, "owner@x.com", "TRADER")
    sys_.strategies.runtimes["macd"].user_id = trader.id
    await link(svc, trader, 500)
    api.calls.clear()
    r = await sys_.strategies.handle_signal("macd", "BUY", "test entry", origin="STRATEGY")
    oid = r["order"]["id"]
    for px in ("1000.5", "1001", "1000.8", "1001.2"):
        await sys_.market.provider.push("RELIANCE", px)
    assert await wait_for(lambda: oid not in sys_.orders.open_orders)
    await asyncio.sleep(0.1)
    await svc.drain()
    texts = api.texts(500)
    assert len(texts) == 1, texts
    t = texts[0]
    assert "filled @" in t and "macd" in t and "strategy signal" in t and "Position now +" in t


async def test_rejections_are_explained_and_throttled(trading):
    sys_ = trading
    svc, api = await setup(sys_)
    trader = await make_user(sys_, "r@x.com", "TRADER")
    sys_.strategies.runtimes["macd"].user_id = trader.id
    await link(svc, trader, 600)
    api.calls.clear()
    await sys_.limits.set("STRATEGY", "macd", "max_position", D(5), None)
    for _ in range(2):
        await sys_.strategies.handle_signal("macd", "BUY", "too big", origin="STRATEGY", quantity=D(50))
    await svc.drain()
    texts = api.texts(600)
    assert len(texts) == 1 and "Order rejected" in texts[0] and "MAX_POSITION" in texts[0]


async def test_commands_respect_ownership_and_roles(trading):
    sys_ = trading
    svc, api = await setup(sys_)
    trader = await make_user(sys_, "t@x.com", "TRADER")
    other = await make_user(sys_, "o@x.com", "TRADER")
    sys_.strategies.runtimes["macd"].user_id = trader.id
    sys_.strategies.runtimes["rsi_ema"].user_id = other.id
    await link(svc, trader, 700)
    # own strategy: pause and resume via buttons
    await svc.handle_update(cb(700, "p:macd"))
    assert sys_.strategies.runtimes["macd"].status == "PAUSED"
    await svc.handle_update(cb(700, "r:macd"))
    assert sys_.strategies.runtimes["macd"].status == "RUNNING"
    # someone else's strategy: refused
    await svc.handle_update(cb(700, "p:rsi_ema"))
    assert sys_.strategies.runtimes["rsi_ema"].status == "RUNNING"
    assert api.answers()[-1] == "not your subscription"
    # /pause by number from /strategies
    await svc.handle_update(msg(700, "/pause 1"))
    assert sys_.strategies.runtimes["macd"].status == "PAUSED"
    # a trader cannot use the kill switch
    await svc.handle_update(msg(700, "/kill"))
    await svc.handle_update(cb(700, "kill:anything"))
    await svc.drain()
    assert "Only an ADMIN" in api.texts(700)[-1]
    assert not sys_.kill_switch.state.get("active")
    # every Telegram action is audited with the telegram actor
    async with sys_.db.session() as s:
        from sqlalchemy import select
        from app.database.models import SystemEvent
        actors = set((await s.execute(select(SystemEvent.actor).where(SystemEvent.event_type == "STRATEGY_STATUS_CHANGED")))
                     .scalars())
    assert "telegram:t@x.com" in actors


async def test_admin_kill_switch_needs_a_fresh_confirmation(trading):
    sys_ = trading
    svc, api = await setup(sys_)
    admin = await make_user(sys_, "admin@x.com", "ADMIN")
    await link(svc, admin, 800)
    await svc.handle_update(cb(800, "kill:not-issued"))
    assert api.answers()[-1].startswith("confirmation expired") and not sys_.kill_switch.state.get("active")
    await svc.handle_update(msg(800, "/kill"))
    await svc.drain()
    markup = [p for m, p in api.calls if m == "sendMessage" and p.get("reply_markup")][-1]["reply_markup"]
    data = markup["inline_keyboard"][0][0]["callback_data"]
    await svc.handle_update(cb(800, data))
    assert api.answers()[-1] == "kill switch activated"
    assert await wait_for(lambda: sys_.controller.status == "SAFE_MODE", 12)
    await asyncio.sleep(0.2)
    await svc.drain()
    texts = api.texts(800)
    assert any("KILL SWITCH ACTIVATED" in t for t in texts) and any("Kill switch completed" in t for t in texts)
    # the same confirmation cannot be replayed
    await svc.handle_update(cb(800, data))
    assert api.answers()[-1].startswith("confirmation expired")
