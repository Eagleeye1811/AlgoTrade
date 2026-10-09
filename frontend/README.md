# AlgoTrade — Frontend

React 18 + TypeScript + Vite 5 + Tailwind 3.4 + lightweight-charts 4.2.3.
The frontend is presentation only: every number comes from the backend REST API or the WebSocket event stream.

## Run

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173  (proxies /api and /ws to http://localhost:8000)
npm run build        # typecheck + production build into dist/
npm run typecheck    # tsc --noEmit
```

The backend must be running on port 8000. The first user to register becomes ADMIN and completes Account Setup.

## Structure

- `src/services/api.ts` – fetch wrapper (JWT from localStorage, 401 → login, backend `detail` errors)
- `src/services/ws.ts` – single auto-reconnecting WebSocket, event_id dedupe, per-type subscriptions, symbol filter
- `src/hooks/` – `useApi` (REST snapshot + throttled refetch on events), `useEvents`, auth and system contexts
- `src/charts/` – `MarketChart` (candles/volume/signal markers), `StrategyChart` (MACD / RSI+EMA / Breakout panes), `LineChart`
- `src/pages/` – Dashboard, Strategies, StrategyDetail, Orders, Positions, Risk, Health (reconciliation), Faults (fault lab, chaos, demo), Audit (trail + causal trace), Settings, Login, AccountSetup

Chart times are shown in IST (exchange time).
