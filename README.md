# AlgoTrade — Fault-Tolerant Algorithmic Trading & Risk Control Platform

**PS-06 · FinTech POC.** *Strategies may fail, but risk controls must remain active.*

> **PAPER TRADING · SIMULATED EXECUTION.** No verified 021 API documentation or credentials were available, so no 021
> adapter was written and no 021 endpoints were invented. Orders execute on the **Paper Broker Simulator**, an
> independent backend venue behind the same `BrokerAdapter` interface a real broker adapter would implement.
> No real money is traded.

## What is real

| Concern | Source |
|---|---|
| Prices / candles | **Real NSE 1-minute bars** from Yahoo Finance's public chart endpoint (`/v8/finance/chart/{SYM}.NS`). Outside NSE hours the feed is a clearly labelled **REPLAY** of the last 5 real sessions (cached in PostgreSQL, raw API snapshot bundled in `backend/data/replay/`). In `auto` mode a scheduler checks the clock every 30s and switches automatically: **LIVE/DELAYED** polling 09:15–15:30 IST Mon–Fri, **REPLAY** otherwise (orders are blocked during the switch, open orders cancelled, strategies re-warmed, state reconciled). The endpoint is public but not an officially documented API, and the UI says so. |
| Signals | RSI+EMA, MACD and Breakout computed on those candles |
| Orders, fills, positions, P&L | Risk Firewall → Order Engine → Paper Broker (fills priced from the tick, with modelled slippage, clamped to the bar's real traded range) → fills ledger → positions → P&L. All persisted in PostgreSQL (NUMERIC/Decimal). |
| Faults | Injected into real components (broker transport, market feed, strategy loop, internal state, process state), with the engine's actual response observed. |

## Architecture (modular monolith)

```
Market Data API ─► MarketDataProvider (Live | Replay | Manual[test]) ─► Candle Engine (1m/5m)
   ─► Strategy Engine (signals only) ─► Order Engine ─► Risk Firewall (+ Latency/Slippage Guard)
   ─► BrokerGateway ─► BrokerAdapter ─► PaperBrokerSimulator (own schema `paper_broker`)
   ─► execution reports ─► fills ledger ─► PositionManager ─► PnL Engine
Resilience: Reconciler · Recovery · Kill Switch · Fault Simulator · Chaos Mode · Demo Mode
State: PostgreSQL = source of truth · Redis = cache, pub/sub, rate limits, locks · React = view only
Events: EventBus ─► Redis pub/sub ─► WebSocket (event_id, correlation_id, source, payload)
```

Key backend paths: `backend/app/market/` (providers, candles), `strategies/`, `engine/` (risk_firewall, order_engine,
position_manager, pnl_engine, system), `brokers/` (base, paper_broker), `resilience/` (reconciliation, kill_switch,
fault_simulator, demo), `api/`. Migrations: `backend/alembic/versions/`.

## Run locally

Prerequisites: PostgreSQL 14+, Redis 7, Python 3.11, Node 20+.

```bash
# backend
cd backend
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # set ALGOTRADE_JWT_SECRET
createdb algotrade
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --port 8000

# frontend
cd frontend && npm install && npm run dev     # http://localhost:5173
```

Register (the first user becomes **ADMIN**), complete **Account Setup** (starting capital, e.g. ₹10,00,000), then
press **Start Trading**.

Docker: `cp .env.example .env` (set the secret), then `docker compose up --build` and open http://localhost:8080.
The compose file was written but not exercised in this environment because the Docker daemon wasn't running.

## Tests

```bash
createdb algotrade_test
cd backend && .venv/bin/python -m pytest -q
```

There are 69 tests against real PostgreSQL (migrated with Alembic) and Redis. They cover auth/RBAC, candle
generation, strategies, risk approval and rejection, virtual margin isolation, order lifecycle, partial fills,
limit/cancel, idempotency, timeout verification, broker outage (fail-closed), reconciliation (lost fills, corruption,
unexplained broker positions), restart recovery, kill switch (including unresolved positions), runaway halting,
latency guard, stale data, daily loss and automatic session switching.

## Judge walkthrough

**Fault Simulation → Demo Mode → Reset demo → Start demo** runs the whole script on the real engine:
real data → strategy fill → position/P&L → risk rejection → API timeout with duplicate prevention → duplicate
attempt → reconciliation mismatch → runaway strategy halted → kill switch → SAFE MODE. Each step can also be
triggered on its own from the Fault Lab. **Run automated chaos mode** chains five faults and ends with a
reconciliation. Every order links to **Trace** (Audit Trail), which answers *"why did this order happen?"*:
signal + indicators → every risk check → lifecycle → fills → position change.

## Risk & safety behaviour

- Every order passes the Risk Firewall inside `OrderEngine.submit()`, the only path to the broker. Rejections carry
  the rule, actual value, allowed value and an explanation.
- Fail closed: an uncertain order status, reconciliation failure, unreachable broker or DB, or stale data all block
  new orders until state is certain again.
- Timeouts are never retried blindly. The engine queries the broker by idempotency key; if the order is found it
  adopts it, and if not it makes one controlled retry with the same key.
- After any restart the system comes up **PAUSED** (or still in SAFE MODE) and reconciles before allowing orders.

## Known limitations (POC)

- One shared paper trading account per deployment; NSE holidays are not modelled (on a holiday the feed switches to LIVE, sees no new bars and reports DELAYED, so orders stay blocked); replay data covers 5 sessions.
- Positions held across a REPLAY → LIVE switch are re-marked at today's live prices, so unrealized P&L can jump; the switch is audited and lists the carried positions.
- Live mode depends on Yahoo's public endpoint (rate limits apply; intraday data may be delayed and is labelled so).
- Partial fills occur naturally only for orders that are large relative to tick volume. The Fault Lab forces them on demand.
