"""L2: exact trade list with net P&L after charges from the published charges table."""
from __future__ import annotations

from decimal import Decimal

from app.api.trades import build_trades
from app.engine.charges import COMPONENTS, ChargesTable
from tests.test_trading import fill_order

D = Decimal
TABLE = ChargesTable({"name": "t", "brokerage": {"pct_of_turnover": 0.03, "max_per_order": 20},
                      "stt": {"sell_pct": 0.025, "buy_pct": 0}, "exchange_txn_pct": 0.00297, "sebi_per_crore": 10,
                      "stamp_duty": {"buy_pct": 0.003, "sell_pct": 0}, "gst_pct": 18, "gst_on": ["brokerage", "exchange_txn", "sebi"]})


def test_order_charges_match_the_table():
    buy = TABLE.order_charges("BUY", D(100000))  # ₹1,00,000 turnover
    assert buy["brokerage"] == D("20.00")          # 0.03% = 30, capped at 20 per order
    assert buy["stt"] == D("0.00") and buy["stamp_duty"] == D("3.00")
    assert buy["exchange_txn"] == D("2.97") and buy["sebi"] == D("0.10")
    assert buy["gst"] == D("4.15")                 # 18% of (20 + 2.97 + 0.10) = 4.1526
    assert buy["total"] == D("30.22")
    sell = TABLE.order_charges("SELL", D(10000))
    assert sell["brokerage"] == D("3.00") and sell["stt"] == D("2.50") and sell["stamp_duty"] == D("0.00")


def test_partial_fills_share_one_brokerage_and_sum_exactly():
    parts = TABLE.allocate("BUY", [("a", D(30000)), ("b", D(30000)), ("c", D(40000))])
    whole = TABLE.order_charges("BUY", D(100000))
    for k in (*COMPONENTS, "total"):
        assert sum(p[k] for p in parts.values()) == whole[k]
    assert parts["a"]["brokerage"] == D("6.00")  # brokerage charged once per order, split by turnover


async def test_trade_list_net_pnl(trading):
    sys_ = trading
    o1 = (await sys_.strategies.handle_signal("macd", "BUY", "in", origin="STRATEGY"))["order"]
    assert await fill_order(sys_, "RELIANCE", "1000", o1["id"])
    o2 = (await sys_.strategies.handle_signal("macd", "SELL", "out", origin="STRATEGY"))["order"]
    assert await fill_order(sys_, "RELIANCE", "1010", o2["id"])
    d = await build_trades(sys_, None, "macd", None)
    assert [t["side"] for t in d["trades"]] == ["BUY", "SELL"]
    st = d["strategies"][0]
    gross = sys_.positions.get("macd", "RELIANCE").realized_pnl
    assert st["realized_gross"] == gross and gross > 0
    charges = sum((t["charges"]["total"] for t in d["trades"]), D(0))
    assert st["charges"] == charges > 0 and st["net_realized"] == gross - charges
    assert d["total"]["net_realized"] == st["net_realized"] and st["open_quantity"] == 0
    # each fill's charges come from the configured table
    t0 = d["trades"][0]
    assert t0["charges"] == sys_.charges.order_charges("BUY", t0["quantity"] * t0["price"])
