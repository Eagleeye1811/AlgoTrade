# AlgoTrade: Algorithmic Trading Platform on the 021 Developer APIs

**Hackathon problem statement 021-03: Algo trading platform** · Team ERROR404

> *Strategies may fail, but risk controls must remain active.*

AlgoTrade lets traders **subscribe to trading strategies** that run against the **021 developer sandbox**
(simulated NSE exchange, virtual ₹10,00,000 per day). The platform takes live prices from the 021 market-data
websocket, **builds 1-minute and 5-minute candles from raw ticks**, runs every subscribed strategy, and sends every
order through a **Risk Firewall that strategy code cannot bypass** to `POST /orders`. It tracks fills, partial
fills and rejections, keeps its positions **reconciled against 021**, and reports **net P&L after charges** per
strategy. A kill switch flattens everything within 10 seconds.

---

## Contents

1. [Highlights](#highlights)
2. [Problem-statement levels](#problem-statement-levels)
3. [Architecture](#architecture)
4. [Strategies (exact rules)](#strategies-exact-rules)
5. [How the platform uses the 021 APIs](#how-the-platform-uses-the-021-apis)
6. [Risk and safety](#risk-and-safety)
7. [Fault Lab, incident console and self-healing autopilot](#fault-lab-incident-console-and-self-healing-autopilot)
8. [Telegram copilot](#telegram-copilot)
9. [The web app](#the-web-app)
10. [Charges and net P&L](#charges-and-net-pl)
11. [Run it](#run-it)
12. [Configuration](#configuration)
13. [Tests](#tests)
14. [Project structure](#project-structure)
15. [Known limitations](#known-limitations)

---

## Highlights

- **Real 021 integration:** REST (login, instruments, orders, trades, positions), the binary market-data websocket
  (byte-exact parser) and the binary orders websocket.
- **Candles from raw ticks:** live 1m/5m candles drawn TradingView-style, with a countdown to the next candle.
- **Three required strategies plus extras:** company Strategy 1 and Strategy 2, our EMA trend strategy, and MACD,
  RSI + EMA and Breakout.
- **Multi-user subscriptions:** each user subscribes to strategies with their own allocation and risk limits. Several
  strategies share one account, each with its own position and P&L, including long and short on the same stock.
- **Risk Firewall** on every order, a **circuit breaker** (halt and flatten), and a **kill switch** with a 10 s deadline.
- **Reconciliation** with 021 every 30 s. It **fails closed**: anything it cannot explain blocks new orders.
- **Fault Lab:** 11 injectable failures with a live incident console and a **self-healing autopilot**.
- **Telegram copilot:** explained alerts and two-tap control from the phone (USP).
- **After-hours session replay**, so the platform can be demonstrated outside NSE hours.
- **Full audit trail:** every signal, risk decision, order, fill and action, with correlation IDs.

## Problem-statement levels

| Level | Requirement | What the platform does | Where |
|---|---|---|---|
| **L1** | User accounts, strategy subscription, candles from ticks | Registration and login (ADMIN/TRADER). Users **subscribe** to a strategy on an NSE stock with a quantity, allocation and limits. **1m and 5m candles are built from 021 ticks**; the EMA strategy uses both in its entry rule. | `api/auth.py`, `api/strategies.py`, `market/candle_engine.py`, `market/providers.py` |
| **L2** | Live orders, fills, partial fills, rejections, aligned positions, exact trades with net P&L | Orders via `POST /orders`; fills read from REST, triggered by the orders websocket plus a 2 s poll; partial fills and rejections handled; reconciliation against `GET /orders`, `/orders/{id}/trades` and `/portfolio/positions`. **Exact trade list with net P&L after charges** on the *Orders, Trades & P&L* page (per-strategy totals, plus the *Trades* tab with every fill, its charges and net), `GET /api/trades` and CSV. | `brokers/o21_adapter.py`, `engine/order_engine.py`, `resilience/reconciliation.py`, `api/trades.py` |
| **L3** | Per-strategy max daily loss, max position and max orders/minute; kill switch within 10 s | All three limits enforced by the Risk Firewall for every strategy. **Kill switch**: SAFE MODE, cancel all, close all, 10-second deadline, elapsed time recorded. | `engine/risk_firewall.py`, `engine/risk_limits.py`, `resilience/kill_switch.py` |
| **L4** | ≥ 3 strategies on one account with their own P&L and position, long and short on the same stock | Any number of subscriptions on one 021 account; per-strategy position and P&L books; for example a long and a short time-entry strategy on RELIANCE at the same time. | `engine/position_manager.py`, `engine/pnl_engine.py` |

## Architecture

```
            Web app (React)                    Telegram (trader's phone)
                  │  REST + WebSocket                 │  Bot API (long polling)
┌─────────────────▼───────────────────────────────────▼─────────────────────────┐
│ FastAPI · auth (JWT, roles) · WebSocket gateway · event bus (fan-out via Redis)│
├───────────────────────────────────────────────────────────────────────────────┤
│ Market Data ─► Candle Engine ─► Strategy Engine ─► Risk Firewall ─► Order Engine ─► Broker Gateway ─► 021 Adapter
│  (integrity     (1m / 5m from    (subscriptions,     (limits, margin,   (lifecycle,      (latency,       (REST + orders WS)
│   filter,        raw ticks)       exits, clock)       circuit breaker)   idempotency)     availability)
│   replay)                                                                                                     │
│ Position Manager · P&L Engine · Charges ◄──────────────── fills ◄─────────────────────────────────────────────┘
│ Safety: Kill Switch · Reconciliation (30 s) · Fault Lab + Autopilot · Health watchdogs · Audit trail
│ Notifications: Telegram copilot
├───────────────────────────────────────────────────────────────────────────────┤
│ PostgreSQL (source of truth)              Redis (rate windows, locks, LTPs, pub/sub)│
└───────────────────────────────────────────────────────────────────────────────┘
                                   ▲
            021 developer sandbox: REST API · market websocket · orders websocket
```

**Stack:** Python 3.11, FastAPI, SQLAlchemy (async) + Alembic, PostgreSQL, Redis, websockets, httpx · React 18,
TypeScript, Vite, Tailwind, lightweight-charts.

## Strategies (exact rules)

Shared platform rules, enforced outside strategy code (`engine/strategy_engine.py`):
- Product `INTRADAY`, exchange `NSE`, book `RL`, validity `Day`; price `0` = market, limits in paise rounded to the
  instrument's tick size; the sign of `qty` is the side.
- One position per subscription; an opposite signal closes the whole position and never reverses in one order.
- The Risk Firewall's latency/slippage/volatility guard may convert a market order into a protective limit 0.1%
  through the LTP when the broker is slow or the market is volatile. The conversion is recorded on the order.
- **Square-off at 15:15 IST** (1-second scheduler): the strategy's working orders are cancelled, then the full
  position is closed with a reduce-only opposite order sized from `squareOffQuantity` of `GET /portfolio/positions`.
  It never closes more than the strategy's own position, because several strategies can share a stock. If 021 shows
  nothing to close, nothing is sent and a reconciliation is run.
- No new entries from candle- or tick-driven signals at or after 15:10 IST.
- **Session-hours switch** (`ALGOTRADE_ENFORCE_MARKET_HOURS`, default `false`). The 021 sandbox accepts orders at any
  time, so by default the platform does **not** apply the time-of-day rules (Strategy 1's 09:15/15:15 schedule, the
  15:10 entry cut-off and the 15:15 square-off), and strategies trade whenever the platform runs. Strategy 1 then
  enters as soon as it runs (once per day), and its position is closed by its user, the kill switch or the next
  square-off. Set the switch to `true` to apply the NSE session rules exactly as written here; the automated tests
  run with `true`.

### Strategy 1: time-based entry and exit (given; `TIME_ENTRY`, `strategies/time_entry.py`)
1. At **09:15 IST**, a market order for the subscription's stock, side (`direction`: 1 buy, −1 sell) and quantity,
   with `product: INTRADAY`. One entry per day. If the platform was not running at 09:15, it enters at the first
   second it is (before 15:15). A rejected entry is retried after 5 s.
2. At **15:15 IST**, the platform square-off described above: cancel its open orders, then close with
   `squareOffQuantity`.

### Strategy 2: 1% breakout from the open, 5% target, 5% stop-loss (given; `OPEN_BREAKOUT`, `strategies/open_breakout.py`)
1. Today's open is the `open` field of the 021 `full` packet for the stock. It is kept across restarts.
2. On every LTP update while flat: LTP ≥ open × 1.01 → **buy**; LTP ≤ open × 0.99 → **sell** (`qty` negative).
   The entry is a limit order at the LTP (paise, tick-rounded).
3. Once the entry fills at average price P, the **platform** manages the exit on every LTP (no broker SL/target):
   long target P × 1.05 and stop P × 0.95; short target P × 0.95 and stop P × 1.05. The exit is an opposite order of
   the same quantity, a **limit at the level**. If the LTP has already passed the level, the limit is set at the LTP
   so it is marketable. An exit limit not filled within 10 s is cancelled and re-sent as a market order.
4. **One filled entry per day.** An entry limit that does not fill is cancelled after 60 s (`limit_order_ttl_s`) and
   does not use up the day's entry. Any position still open is squared off at 15:15.

### Strategy 3: our own, EMA crossover + 5m trend (`EMA_TREND`, `strategies/ema_trend.py`)
* **Data:** closed 1m and 5m candles built by the platform from 021 ticks; the rules are evaluated at each 1m
  candle close.
* **Indicators:** `fast` = EMA(9) and `slow` = EMA(21) of 1m closes (each EMA seeded with the SMA of its first
  `period` closes); `trend` = EMA(20) of 5m closes; `close5` = close of the last closed 5m candle. All periods can be
  changed per subscription.
* **Long entry:** `fast` crosses above `slow` (previously `fast ≤ slow`, now `fast > slow`) and `close5 > trend`,
  while flat.
* **Short entry:** `fast` crosses below `slow` and `close5 < trend`, while flat.
* **Exit:** the opposite crossover (always closes, whatever the 5m trend); stop-loss 1% and take-profit 2% from the
  average entry price, checked on every tick (market orders); square-off at 15:15.
* **No entry** from a candle that closes at or after 15:10 IST, or before enough closed 5m candles exist for the trend
  EMA. A crossover then only closes a position.
* **Size:** the subscription's fixed quantity (default 10) per entry; never added to.

**Also available:** MACD crossover, RSI + EMA and Breakout (`backend/app/strategies/`), with the same platform exits.

**Capital is never allocated automatically.** The first ADMIN gets three subscriptions at startup (`time_entry`,
`open_breakout`, `ema_trend`; see `ALGOTRADE_DEFAULT_STRATEGIES`) with ₹0 allocated. A strategy cannot open positions
until its user gives it an allocation (when subscribing, or Strategies → Configure). Allocations never exceed the
account capital in total.

## How the platform uses the 021 APIs

| Concern | Implementation (`backend/app/brokers/`) |
|---|---|
| **Login** | `POST /auth/token`; one token shared by every call, because a second login revokes the first. Logins are serialized. A 401 triggers a fresh login, retried up to 3 times with backoff. Repeated revocations (3 in 5 minutes) are reported as a **session conflict** ("another session is logging in to this 021 account"). Credentials come only from `backend/.env`. |
| **Instruments** | `GET /instruments` (gzipped CSV), downloaded once per IST day and cached in `data/instruments/`; NSE cash symbol → token, tick size and circuit limits. |
| **Market data** | One `market` websocket; ephemeral key from `GET /websocket/ephemeral-key`; `{"Task":"subscribe","Mode":"full",...}` in batches of ≤ 100. Binary big-endian `full` packets are parsed per the API Guide (LTP, OHLC, volume, trade time). A tick is emitted when the LTP or volume changes. Reconnects with backoff and a fresh key (021 restarts at 08:00 IST). |
| **Data integrity** | The sandbox interleaves a stale, time-frozen stream. A packet is used only if its last-trade time is not older than the newest accepted one for that stock and, on the same day, its cumulative volume does not fall. After a restart the filter starts from the newest saved candle. |
| **After hours** | Outside Mon–Fri 09:15–15:30 IST, the **most recent recorded 021 session** is replayed in real time through the same candle engine and strategies: each candle becomes 4 ticks, 15 s apart, aligned to the clock (`ALGOTRADE_AFTER_HOURS_REPLAY`, `ALGOTRADE_REPLAY_STEP_S`). The status bar says *"after hours: replay of the 021 session of ‹date›"*. If there is nothing to replay, the sandbox's own stream is used (`ALGOTRADE_O21_SANDBOX_STREAM_AFTER_S`). Live 021 data takes over as soon as an exchange-timed packet arrives. |
| **Candles** | Built by the platform from ticks, closed by the clock 3 s after each period ends, and saved to `market_bars` with their source (`ticks`, `021-replay`, `021-sandbox`), because 021 has no candle-history endpoint. |
| **Orders** | `POST /orders`; cancel with `DELETE /orders/{id}` and `{exchange, token, product}`. |
| **Fills and status** | REST is the source of truth. Each orders-websocket event (binary TC 4/8) re-reads that order and its trades; open orders are also polled every 2 s because events are not replayed. Trades are deduplicated by `tradeId`. The sandbox's `tradeTime` (shifted by the 1980-epoch offset) is normalized. |
| **Failures** | 429 → wait and retry. 500/503/timeout on GET/DELETE → retry with backoff. `POST /orders` is **never resent blindly**: 021 has no client order ID, so the order book is searched for an unclaimed order with the same token, side, quantity and price placed after the send. If found it is adopted; if confirmed absent, a 500 with an error text is a risk rejection; otherwise one controlled retry. 422 (021 safe mode) and 400 are rejections. |
| **Positions** | `GET /portfolio/positions` for reconciliation (net per stock) and the square-off quantity. |
| **Funds** | 021 has no funds endpoint, so cash is not reconciled; the platform tracks per-strategy virtual margin against allocations. |

## Risk and safety

- **One path to the broker:** every order passes the Risk Firewall inside `OrderEngine.submit()`. A rejection records
  the rule, the actual value, the allowed value and an explanation.
- **Per-strategy limits (L3):**
  - **max loss for the day** (default 5% of the allocation; realized today plus unrealized);
  - **max position** (shares);
  - **max orders per minute**;
  - max exposure (default = allocation).

  Hitting the daily-loss limit blocks risk-increasing orders for that strategy only; closing orders remain allowed.
- **Global limits:** daily loss, gross exposure and order rate; latency, slippage and volatility guards; stale-data
  and duplicate-order protection.
- **Circuit breaker:** after 5 rejections in 60 s, a runaway strategy is **halted and flattened**. It is set HALTED,
  its working orders are cancelled, and its position is closed with a reduce-only market order, re-sent until flat
  (10 s deadline). Other strategies keep trading. A user must resume the halted strategy deliberately.
- **Kill switch:** SAFE MODE first, then all working orders cancelled in parallel, then a reduce-only MARKET close
  per strategy book in parallel. Partial or rejected closes are re-sent until the **10-second deadline**; anything
  left is reported as UNRESOLVED. Available from the web app and from Telegram (ADMIN).
- **Reconciliation** every 30 s (5 s after a failed run). It compares orders, fills and positions with 021 and
  repairs what 021 proves: missing fills and stale statuses are fixed, and the position cache is rebuilt from fills.
  Anything it cannot explain **blocks new orders**. If 021 holds trades the platform never recorded (for example
  after a database reset) while positions still match, an ADMIN can **acknowledge them as external**. This is
  audited, and anything new still blocks.
- **Fail closed:** an uncertain order, a reconciliation mismatch, an unreachable broker or database, stale data or
  a session conflict blocks new orders until state is certain again.
- **After any restart**, the system comes up **PAUSED** (or in SAFE MODE) and reconciles before allowing orders.
- **Risk page:**
  - live meters for daily loss, max position size and orders per minute;
  - manual limit editing (ADMIN);
  - an **AI Risk Advisor** that recommends the max daily loss, max position size and max orders per minute from
    age, monthly income and risk appetite, and applies them in one click (`/api/risk/recommendations`).

## Fault Lab, incident console and self-healing autopilot

**11 scenarios** inject a fault at a real component boundary and record what the engine actually did:

| Area | Scenarios |
|---|---|
| Broker | API failure, network timeout, order rejection, high latency, lost execution report (position mismatch) |
| Strategy | Runaway strategy |
| Market | Stale market data |
| Orders and state | Duplicate order attempt, internal state corruption, server restart, risk rejection |

*Partial fill* needs the local simulator and is disabled on 021. On 021, scenarios place real sandbox orders, with
test quantities capped at the strategy's max position. Each scenario has its own target-strategy picker and Inject
button. Chaos mode runs several scenarios back to back.

**Live incident console.** While a fault runs, the page shows:
- a running stopwatch;
- a phase tracker: Inject → Respond → Recover → Final → Autopilot;
- a **live system map** of 8 components (market data, strategy, Risk Firewall, order engine, 021 broker,
  positions, reconciliation, trading engine), each coloured by the steps that touch it;
- the event stream as it happens;
- at the end, a **scorecard**: time to detect, time to recover, total duration, events and errors, final state and
  the autopilot's decision.

**Self-healing autopilot** (ADMIN toggle, persisted; `PUT /api/faults/autopilot`). After every fault it:
1. runs a fresh reconciliation against 021;
2. checks that the broker is reachable, market data is LIVE or REPLAY, there are no order blocks, and the system is
   not in SAFE MODE;
3. resumes trading only if **every** check passes and the fault left trading PAUSED (it was RUNNING before), after a
   5-second countdown and a re-check.

It never leaves SAFE MODE and never resumes a strategy halted by the circuit breaker; instead it lists what a human
must do. Every decision is audited (`AUTOPILOT_DECISION`).

## Telegram copilot

`app/notifications/telegram.py` · optional · enabled by `ALGOTRADE_TELEGRAM_BOT_TOKEN` (from @BotFather). The bot
uses long polling (`getUpdates`), so it needs no public URL.

- **Linking:** Settings → Telegram copilot → *Connect Telegram* creates a one-time code (10 minutes, single use) and
  opens `t.me/<bot>?start=<code>`. The bot binds that **private** chat to the user. Unlinked chats and group chats
  get no data.
- **Explained alerts** (each category can be switched off; `/mute` silences all):

  | Category | What you get |
  |---|---|
  | **Trades** | One message per order with average price, number of fills, strategy, reason (signal, stop-loss, target, square-off, kill switch, halt and flatten, Fault Lab), position and P&L today |
  | **Rejections** | Risk Firewall or broker rule and reason; at most one per strategy and rule per minute |
  | **Risk** | Circuit-breaker halt and the halt-and-flatten result; 80% of the daily loss limit used |
  | **System** | Kill switch activated and completed (everyone). ADMINs also get order blocks, trading-status changes, component failures and unresolved positions |
  | **Summary** | End-of-day P&L per strategy, weekdays at 15:35 IST |

- **Commands:** `/status`, `/pnl`, `/positions`, `/strategies` (inline Pause/Resume buttons), `/pause <n|id>`,
  `/resume <n|id>`, `/summary`, `/mute`, `/unmute`, `/unlink`, `/help`, and **`/kill`**. `/kill` is ADMIN-only and
  needs a confirm button that is valid for 30 s and works once.
- **Safety:** the same authorization as the web app (a trader controls only their own strategies). Every action is
  audited as `telegram:<email>`. Outgoing messages are rate-limited (one per chat per second, retry on HTTP 429), and
  the bot token is never logged.

## The web app

| Page | What it shows |
|---|---|
| **Dashboard** | Account KPIs; a live candle chart (1m/5m) with countdown and strategy trade markers (hover a candle for full signal details); a watchlist of 15 NSE stocks; subscribed strategies with live P&L; recent orders; the live event stream |
| **Strategies** | Subscribe to a strategy; configure the allocation, parameters and the three limits; pause, resume, unsubscribe; per-strategy detail chart with indicators |
| **Orders, Trades & P&L** | Net P&L summary (all days or a chosen date, with a calendar); Orders tab with quick filters (All, Open, Filled, Partially filled, Rejected, Cancelled) plus strategy, side and origin filters; Trades tab with every fill, its charges, net, slippage, latency and source; CSV everywhere |
| **Positions** | Per-strategy and per-stock positions and P&L |
| **Risk** | Limit meters, manual limits, AI Risk Advisor, per-strategy risk cards, the latency/slippage guard, the risk-decision log |
| **Reconciliation & Health** | Component health, reconciliation runs, 021 state compared with internal state, acknowledgement of external trades |
| **Fault Simulation** | Fault Lab, chaos mode, incident console, autopilot, run history |
| **Audit Trail** | Every event with severity, source, strategy and actor; trace by correlation ID |
| **Settings / Account** | Trading account, market-data status, users and roles, Telegram copilot |

## Charges and net P&L

Net P&L = gross realized P&L − charges, from `backend/data/charges.json`. The charges are brokerage per executed
order (% of turnover, capped per order), STT, exchange transaction charge, SEBI fee, stamp duty, and GST on
(brokerage + exchange + SEBI). An order's charges are split across its partial fills.
**The values are a placeholder** (a typical NSE intraday schedule) because the API Guide has no charges table.
Replace the file when the official table is published; no code change is needed.

## Run it

**Prerequisites:** PostgreSQL 14+, Redis 7, Python 3.11, Node 20+, and a 021 developer account (UCC + password).

```bash
# 1. backend
cd backend
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp ../.env.example .env            # set ALGOTRADE_JWT_SECRET, ALGOTRADE_O21_USERNAME, ALGOTRADE_O21_PASSWORD
createdb algotrade
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --port 8000

# 2. frontend (new terminal)
cd frontend
npm install
npm run dev                        # http://localhost:5173   (add `-- --host` to open it from other devices)
```

**Docker:** put the same variables in `./.env`, then run `docker compose up --build`. The app is at
http://localhost:8080 and the API at http://localhost:8000.

**First use:**
1. Register. The first user becomes **ADMIN**; later users are TRADERs.
2. Complete **Account Setup** (capital the platform may allocate, at most ₹10,00,000).
3. **Subscribe** to strategies and give each one an allocation.
4. Press **Start trading**.

> **One session per 021 account.** Do not log in to the same UCC from the 021 web app, Postman, scripts or a second
> backend while the platform runs. Every 021 login revokes the previous token, and the platform will report a
> session conflict and block orders. Keep the 021 account free of manual trades: reconciliation blocks trading on
> positions it did not place.

## Configuration

All settings are environment variables prefixed `ALGOTRADE_` (see `backend/app/config.py` and `.env.example`).

| Variable | Default | Purpose |
|---|---|---|
| `ALGOTRADE_O21_USERNAME` / `_PASSWORD` | — | 021 sandbox credentials (required) |
| `ALGOTRADE_JWT_SECRET` | — | Signing key for user sessions (required) |
| `ALGOTRADE_DATABASE_URL` | `postgresql+asyncpg://localhost:5432/algotrade` | PostgreSQL |
| `ALGOTRADE_REDIS_URL` / `_REDIS_NAMESPACE` | `redis://localhost:6379/0` / `algotrade` | Redis and its key/channel prefix |
| `ALGOTRADE_SYMBOLS` | 15 NSE large caps | Tradable stocks |
| `ALGOTRADE_DEFAULT_STRATEGIES` | `time_entry,open_breakout,ema_trend` | Subscriptions created for the first ADMIN (₹0 allocated) |
| `ALGOTRADE_ENFORCE_MARKET_HOURS` | `false` | Apply the NSE session rules |
| `ALGOTRADE_AFTER_HOURS_REPLAY` / `_REPLAY_STEP_S` | `true` / `15` | Replay the recorded 021 session after hours |
| `ALGOTRADE_O21_SANDBOX_STREAM_AFTER_S` | `20` | Fall back to the sandbox stream when nothing else flows (0 = off) |
| `ALGOTRADE_RECONCILE_INTERVAL_S` | `30` | Reconciliation period |
| `ALGOTRADE_TELEGRAM_BOT_TOKEN` | empty | Enables the Telegram copilot |
| `ALGOTRADE_TELEGRAM_SUMMARY_AT` | `15:35` | Daily summary time (IST) |
| `ALGOTRADE_CHARGES_TABLE_PATH` | `data/charges.json` | Charges schedule |

## Tests

```bash
createdb algotrade_test
cd backend && .venv/bin/python -m pytest -q
```

The tests run offline against real PostgreSQL and Redis. They use their own database and Redis namespace, so they
never touch a running instance.

- **021 integration:**
  - byte-exact websocket frames (market `full`/`ltp`/snapshot/option chain; orders TC 4/8);
  - the integrity filter, after-hours replay and sandbox fallback;
  - the adapter against a fake 021 exchange: order body, tick rounding, fills, partial fills, ambiguous 500s,
    idempotency, positions, cancel, `tradeTime` normalization;
  - the transport's retry, re-login and session-conflict policy.
- **Engine** (on a local exchange simulator, `broker_mode=paper`, because a real exchange cannot be told to lose a
  response, fill 40% or go down on demand):
  - subscriptions and ownership, candles;
  - the three required strategies end to end;
  - all risk limits, partial fills, idempotency, timeout verification, outages;
  - reconciliation and external-trade acknowledgement;
  - restart recovery, kill switch within 10 s, circuit breaker halt and flatten;
  - long and short books on the same stock; charges and net P&L.
- **Fault Lab and autopilot:** scenario timelines, faults disarmed on abort, test orders capped at the max position,
  autopilot resuming only when every check passes and never overriding SAFE MODE.
- **Telegram:** one-time linking, private chats only, one alert per order, rejection throttling, ownership and role
  checks, kill-switch confirmation.

## Project structure

```
backend/
  app/
    api/            REST endpoints (auth, account, market, strategies, orders, trades, risk, resilience, audit, telegram)
    brokers/        021 client, protocol parser, instruments, adapter; paper simulator (tests)
    core/           event bus, audit, health, Redis, security, WebSocket gateway
    database/       SQLAlchemy models and sessions
    engine/         strategy engine, risk firewall and limits, order engine, positions, P&L, charges, controller
    market/         market-data providers, candle engine, market-data service
    notifications/  Telegram copilot
    resilience/     kill switch, reconciliation, fault simulator + autopilot, demo mode
    strategies/     Strategy 1, Strategy 2, EMA trend, MACD, RSI + EMA, Breakout, indicators
  alembic/          database migrations
  data/             charges table, instrument cache
  tests/
frontend/src/
  pages/            Dashboard, Strategies, Orders, Positions, Risk, Health, Faults, Audit, Settings, ...
  components/       charts, incident console, Telegram panel, risk advisor, trades summary, ...
```

## Known limitations

- **One 021 account (UCC) is shared by all users.** Users are separated inside the platform (their own
  subscriptions, positions, P&L and limits). On 021 their positions net into one position per stock.
- **Charges are a placeholder** until the official table is published.
- **No funds endpoint at 021**, so cash is not reconciled; margin is the platform's virtual margin.
- **After hours the market data is a replay** of the recorded session. Orders still go to the 021 sandbox and fill at
  its own prices, so fills can differ from the replayed price.
- **021 does not replay order events**, so open orders are also polled; and a second login anywhere revokes the
  platform's token (see *One session per 021 account* above).
