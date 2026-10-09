# AlgoTrade — Algo Trading Platform on the 021 Developer APIs (PS 021-03)

*Strategies may fail, but risk controls must remain active.* Users subscribe to strategies; the platform takes prices
from the **021 market-data websocket**, builds 1-minute and 5-minute candles from those ticks, runs the strategies,
and sends every order through a Risk Firewall the strategy code cannot bypass to **`POST /orders`** on the 021
developer sandbox (virtual ₹10,00,000 per day, simulated exchange).

> **Validation status.** The 021 integration is implemented from the *021 Trade Hackathon – API Guide* and tested
> against a fake 021 exchange that speaks the Guide's request/response formats and byte-exact websocket frames.
> It has **not yet been run against the live sandbox** — that needs the team's UCC and password in `backend/.env`.

## Levels

| Level | What the platform does | Where |
|---|---|---|
| **L1** | Users register and **subscribe** to any strategy on an NSE stock with their own quantity, allocation and risk limits. **1m and 5m candles are built from 021 ticks**; the EMA strategy uses both in its entry rule. | `api/strategies.py`, `market/candle_engine.py`, `market/providers.py` |
| **L2** | Orders via `POST /orders`; fills tracked from REST (`/orders/{id}`, `/orders/{id}/trades`) triggered by the orders websocket plus a 2 s poll; partial fills and rejections handled; reconciliation against `GET /orders` and `GET /portfolio/positions`. **Exact trade list with net P&L after charges** (page *Trades & Net P&L*, `GET /api/trades`, CSV). | `brokers/o21_adapter.py`, `engine/order_engine.py`, `resilience/reconciliation.py`, `api/trades.py` |
| **L3** | Per-strategy **max loss for the day**, **max position**, **max orders per minute**, enforced in the Risk Firewall. **Kill switch**: SAFE MODE, cancel all, close all, 10-second deadline with the elapsed time recorded. | `engine/risk_firewall.py`, `resilience/kill_switch.py` |
| **L4** | Several subscriptions on **one account**, each with its own position and P&L, including one long and one short in the same stock. | `engine/position_manager.py`, `engine/pnl_engine.py` |

## Strategies (exact rules)

Shared platform rules, enforced outside strategy code (`engine/strategy_engine.py`):
- Product `INTRADAY`, exchange `NSE`, book `RL`, validity `Day`; price `0` = market, limits in paise rounded to the
  instrument's tick size; the sign of `qty` is the side.
- One position per subscription; an opposite signal closes the whole position (never reverses in one order).
- Market orders may be converted by the Risk Firewall's latency/slippage/volatility guard into a protective limit
  0.1% through the LTP when the broker is slow or the market is volatile (recorded on the order).
- **Square-off at 15:15 IST** (1-second scheduler): the strategy's working orders are cancelled, then the full
  position is closed with a reduce-only opposite order sized from `squareOffQuantity` of `GET /portfolio/positions`
  (never more than the strategy's own position, because several strategies can share a stock). If 021 shows nothing
  to close, nothing is sent and a reconciliation is run.
- No new entries from candle- or tick-driven signals at or after 15:10 IST.
- **Session-hours switch** (`ALGOTRADE_ENFORCE_MARKET_HOURS`, default `false`): the 021 sandbox accepts orders at any
  time, so by default the platform does **not** apply the time-of-day rules (09:15/15:15 schedule of Strategy 1,
  15:10 entry cut-off, 15:15 square-off) and strategies trade whenever the platform runs; Strategy 1 then enters as
  soon as it runs (once per day) and its position is closed by its user, the kill switch or the next square-off.
  Set it to `true` to apply the NSE session rules exactly as written in this section (the automated tests run with
  `true`).

### Strategy 1 — time-based entry and exit (given; `TIME_ENTRY`, `strategies/time_entry.py`)
1. At **09:15 IST** a market order for the subscription's stock, side (`direction`: 1 buy, −1 sell) and quantity,
   `product: INTRADAY`. One entry per day. If the platform was not running at 09:15 it enters at the first second it
   is (before 15:15). An entry that is rejected is retried after 5 s.
2. At **15:15 IST** the platform square-off above (cancel its open orders, close with `squareOffQuantity`).

### Strategy 2 — 1% breakout from the open, 5% target, 5% stop-loss (given; `OPEN_BREAKOUT`, `strategies/open_breakout.py`)
1. Today's open = the `open` field of the 021 `full` packet for the stock.
2. On every LTP update while flat: LTP ≥ open × 1.01 → **buy**; LTP ≤ open × 0.99 → **sell** (`qty` negative).
   The entry is a limit order at the LTP (paise, tick-rounded).
3. Once the entry fills at average price P, the **platform** manages the exit on every LTP (no broker SL/target):
   long target P × 1.05, stop P × 0.95; short target P × 0.95, stop P × 1.05. The exit is an opposite order of the
   same quantity, a **limit at the level** (if the LTP has already passed the level the limit is set at the LTP so it is
   marketable). An exit limit not filled within 10 s is cancelled and re-sent as a market order.
4. **One filled entry per day.** An entry limit that does not fill is cancelled after 60 s (`limit_order_ttl_s`) and
   does not use up the day's entry. Any position still open is squared off at 15:15.

### Strategy 3 — our own: EMA crossover + 5m trend (`EMA_TREND`, `strategies/ema_trend.py`)
* **Data:** closed 1m and 5m candles built by the platform from 021 ticks; rules evaluated at each 1m candle close.
* **Indicators:** `fast` = EMA(9), `slow` = EMA(21) of 1m closes (EMA seeded with the SMA of the first `period`
  closes); `trend` = EMA(20) of 5m closes; `close5` = close of the last closed 5m candle.
* **Long entry:** `fast` crosses above `slow` (previous `fast ≤ slow`, now `fast > slow`) and `close5 > trend`, while flat.
* **Short entry:** `fast` crosses below `slow` and `close5 < trend`, while flat.
* **Exit:** opposite crossover (always closes, whatever the 5m trend); stop-loss 1% and take-profit 2% from the
  average entry price, checked on every tick (market orders); square-off at 15:15.
* **No entry** from a candle that closes at/after 15:10 IST or before 20 closed 5m candles exist (a crossover then only closes).
* **Size:** the subscription's fixed quantity (default 10) per entry; never added to.

Also available for subscription: RSI + EMA, MACD crossover, Breakout (`backend/app/strategies/`), with the same
platform exits. The first ADMIN gets three subscriptions at startup: `time_entry` (TCS, 1 share, buy),
`open_breakout` (RELIANCE, 1 share) and `ema_trend` (RELIANCE, 10 shares) — `ALGOTRADE_DEFAULT_STRATEGIES` —
with **no capital allocated**: no allocation is ever assigned automatically. A strategy cannot open positions until
its user gives it an allocation (Strategies → Configure, or when subscribing); allocations never exceed the account
capital in total.

## How the platform uses the 021 APIs

| Concern | Implementation (`backend/app/brokers/`) |
|---|---|
| Login | `POST /auth/token` once; the token is shared by every call (a second login revokes the first). Logins are serialised; a 401 triggers exactly one re-login. Credentials only from `backend/.env`. |
| Instruments | `GET /instruments` (gzipped CSV) downloaded once per IST day, cached in `data/instruments/`; NSE cash (`NSECM`, `STK`) symbol → token, tick size, circuits. |
| Market data | One `market` websocket, ephemeral key from `GET /websocket/ephemeral-key`, `{"Task":"subscribe","Mode":"full",...}` in batches of ≤100. Binary big-endian `full` packets parsed per the Guide (LTP, OHLC, volume, trade time). A tick is emitted when LTP or volume changes; tick volume = volume increase. Reconnects with backoff and a fresh key (08:00 IST restart). Integrity filter: a packet is used only if its last-trade time is not older than the newest accepted for that stock and, the same day, its cumulative volume does not fall (the sandbox interleaves a stale, time-frozen stream). **Sandbox stream fallback** (`ALGOTRADE_O21_SANDBOX_STREAM_AFTER_S`, default 20, 0 = off): if no packet passed the filter for that long (after hours), the sandbox's stream is used anyway, stamped with its receive time, source `021-sandbox`, falling volume ignored, session open/previous close kept from the real session; exchange-timed packets take over again immediately. **After-hours session replay** (`ALGOTRADE_AFTER_HOURS_REPLAY`, default `true`; takes precedence over the sandbox stream): outside Mon–Fri 09:15–15:30 IST, the most recent recorded 021 session (the 1m candles built from exchange-timed 021 ticks) is replayed in real time through the same candle engine and strategies: each candle becomes 4 ticks (open, low/high, high/low, close) `ALGOTRADE_REPLAY_STEP_S` (15) seconds apart, stamped with the replay time, source `021-replay`, looping at the end; the status bar under the header says "after hours: replay of the 021 session of <date>". Orders still go to the 021 sandbox and fill at its own prices, so fills can differ from the replayed price. Live 021 data takes over as soon as an exchange-timed packet arrives. |
| Candles | Built by the platform from these ticks; closed by the clock 3 s after the period ends even if the stock is quiet; persisted (`market_bars`) and reloaded at startup, because 021 has no candle-history endpoint. |
| Orders | `POST /orders` (see platform rules), `DELETE /orders/{id}` with `{exchange, token, product}`. |
| Fills / status | REST is the source of truth. Each `orders` websocket event (binary TC 4/8) re-reads that order and its trades; open orders are also polled every 2 s because events are not replayed. Trades are deduplicated by `tradeId`. |
| Failures | 429 → wait and retry. 500/503/timeout on GET/DELETE → retry with backoff. On `POST /orders` → never resent blindly: the order book is searched for an unclaimed order with the same token, side, quantity and price placed after the send; if found it is adopted, if confirmed absent a 500 with an error text is a risk rejection, otherwise one controlled retry. 422 (021 safe mode) and 400 are rejections. |
| Positions | `GET /portfolio/positions` for reconciliation (net per stock) and the square-off quantity. |
| Funds | 021 has no funds endpoint: cash is not reconciled; the platform tracks per-strategy virtual margin against its allocations. |

## Risk & safety behaviour

- Every order passes the Risk Firewall inside `OrderEngine.submit()`, the only path to the broker. Rejections carry
  the rule, actual value, allowed value and an explanation.
- Per-strategy limits: max loss for the day (default 5% of the current allocation unless set explicitly; realized
  today + unrealized), max position
  (shares), max orders per minute, max exposure (default = allocation). Hitting the daily-loss limit blocks
  risk-increasing orders for that strategy only; closing orders remain allowed.
- Runaway strategy: pending-order protection, identical-order suppression (5 s) and the per-strategy order-rate limit
  reject the flood; after 5 rejections in 60 s the **circuit breaker halts and flattens that strategy only**: it is set
  HALTED, its working orders are cancelled and its position is closed with a reduce-only market order (re-sent until
  flat, 10 s deadline). Other strategies keep trading; a user must resume the halted strategy deliberately.
- Global limits: daily loss, gross exposure, order rate; latency, slippage and volatility guards.
- Kill switch: SAFE MODE first, all working orders cancelled in parallel, a reduce-only MARKET close per strategy book
  in parallel, partial/rejected closes re-sent until the 10-second deadline, anything left reported as UNRESOLVED.
- Fail closed: an uncertain order, a reconciliation mismatch (including positions at 021 that the platform did not
  place), an unreachable broker or DB, or stale data block new orders until state is certain again.
- After any restart the system comes up **PAUSED** (or in SAFE MODE) and reconciles before allowing orders.

## Charges

Net P&L = gross realized P&L − charges, from `backend/data/charges.json`: brokerage per executed order (% of
turnover, capped per order), STT, exchange transaction charge, SEBI fee, stamp duty, GST on (brokerage + exchange +
SEBI); an order's charges are split over its partial fills. **The values are a placeholder** (a typical NSE intraday
schedule): the API Guide contains no charges table. Replace the file when the official table is published.

## Run locally

Prerequisites: PostgreSQL 14+, Redis 7, Python 3.11, Node 20+, a 021 developer account (UCC + password).

```bash
# backend
cd backend
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp ../.env.example .env         # set ALGOTRADE_JWT_SECRET, ALGOTRADE_O21_USERNAME, ALGOTRADE_O21_PASSWORD
createdb algotrade
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --port 8000

# frontend
cd frontend && npm install && npm run dev     # http://localhost:5173
```

Register (the first user becomes **ADMIN**), complete **Account Setup** (capital the platform may allocate, at most
₹10,00,000), give each strategy you want to run an allocation (Strategies → Configure), press **Start Trading**
during NSE hours. Do not log in to the same UCC from Postman or other scripts
while the platform runs (that revokes its token), and keep the 021 account free of manual trades (reconciliation
blocks trading on positions it did not place).

## Tests

```bash
createdb algotrade_test
cd backend && .venv/bin/python -m pytest -q
```

Automated tests run offline against real PostgreSQL and Redis:
- **021 integration:** byte-exact websocket frames (market `full`/`ltp`/snapshot/option-chain, orders TC 4/8),
  the adapter against a fake 021 exchange (order body, tick rounding, fills, partials, ambiguous 500s, idempotency,
  positions, cancel), and the transport's retry/re-login policy.
- **Engine:** a local exchange simulator (`broker_mode=paper`, tests only) because a real exchange cannot be told to
  lose a response, fill 40% or go down on demand: subscriptions and ownership, candles, the three required strategies
  end to end (09:15 entry, 15:15 square-off with order cancel, breakout target/stop with level exits, one entry per
  day), risk limits including per-strategy daily loss, partial fills, idempotency, timeout verification, outages,
  reconciliation, restart recovery, kill switch within 10 s, long and short books on the same stock, charges and net P&L.

## Fault Lab

Every scenario injects a fault at a real component boundary — the broker adapter (outage, lost response, rejection,
latency, lost execution report), the market feed, strategy logic or internal state — and records what the engine
did. On 021, scenarios place real sandbox orders. *Partial fill* needs the local simulator and is disabled on 021.