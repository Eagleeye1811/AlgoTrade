import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useSystem } from '../hooks/system'
import { useApi } from '../hooks/useEvents'
import { fmtINR, fmtQty, fmtSigned, pnlClass } from '../services/format'
import MarketChart from '../charts/MarketChart'
import EventFeed from '../components/EventFeed'
import Watchlist from '../components/Watchlist'
import OrdersTable from '../components/OrdersTable'
import { Empty, Loading, Meter, Panel, Side, Stat, StatusBadge } from '../components/ui'

export default function Dashboard() {
  const { overview } = useSystem()
  const nav = useNavigate()
  const symbols = useApi<any[]>('/market/symbols')
  const [scope, setScope] = useState<'mine' | 'all'>('mine')
  const strategies = useApi<any[]>(`/strategies${scope === 'mine' ? '?mine=true' : ''}`,
    ['strategy.update', 'strategy.status', 'strategy.config', 'position.update', 'strategy.signal', 'pnl.update'], 1000)
  const orders = useApi<any[]>('/orders?limit=8', ['order.update'], 700)
  const risk = useApi<any>('/risk', ['risk.decision', 'pnl.update'], 3000)
  const [chartSym, setChartSym] = useState<string | undefined>(undefined)
  const symList = (symbols.data || []).map((s) => s.symbol)
  if (!overview) return <Loading />
  const p = overview.portfolio
  return (
    <div className="space-y-3">
      <Panel bodyClass="grid grid-cols-2 sm:grid-cols-5 xl:grid-cols-10 divide-x divide-line">
        <Stat label="Total capital" value={fmtINR(p.starting_capital)} />
        <Stat label="Equity" value={fmtINR(p.equity)} />
        <Stat label="Available capital" value={fmtINR(p.available_capital)} />
        <Stat label="Margin used" value={fmtINR(p.margin_used)} sub={p.reserved_margin > 0 ? `+${fmtINR(p.reserved_margin)} reserved` : undefined} />
        <Stat label="Overall exposure" value={fmtINR(p.exposure)} />
        <Stat label="Today's P&L" value={fmtSigned(p.today_pnl)} className={pnlClass(p.today_pnl)} />
        <Stat label="Realized P&L" value={fmtSigned(p.realized)} className={pnlClass(p.realized)} />
        <Stat label="Unrealized P&L" value={fmtSigned(p.unrealized)} className={pnlClass(p.unrealized)} />
        <Stat label="Active positions" value={p.active_positions} />
        <Stat label="Orders today" value={p.orders_today} sub={`${p.open_orders} open`} />
      </Panel>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_330px]">
        <Panel className="overflow-hidden">
          <MarketChart symbols={symList} symbol={chartSym} onSymbolChange={setChartSym} />
        </Panel>
        {/* on xl the watchlist matches the chart's height and scrolls internally */}
        <div className="relative h-[460px] xl:h-auto">
          <Panel className="absolute inset-0 flex flex-col overflow-hidden" bodyClass="min-h-0 flex-1">
            <Watchlist selected={chartSym} onSelect={setChartSym} />
          </Panel>
        </div>
      </div>

      <Panel title={scope === 'mine' ? 'My subscribed strategies · live P&L' : 'All strategies on the account · live P&L'}
        right={<>
          <div className="flex overflow-hidden rounded border border-line text-[11px]">
            {(['mine', 'all'] as const).map((k) => <button key={k} onClick={() => setScope(k)}
              className={`px-2 py-0.5 ${scope === k ? 'bg-info text-white' : 'text-muted hover:text-text'}`}>{k === 'mine' ? 'Mine' : 'All'}</button>)}
          </div>
          <Link to="/strategies" className="text-info text-[11px]">Subscribe / manage →</Link></>}>
        {!strategies.data ? <Loading /> : strategies.data.length === 0 ? <Empty>No subscriptions yet — <Link className="text-info" to="/strategies">subscribe to a strategy</Link>.</Empty> : (
          <div className="overflow-x-auto"><table className="tbl">
            <thead><tr><th>Strategy</th><th>Symbol</th><th>Status</th><th>Signal</th><th>Position</th><th className="text-right">P&amp;L today</th><th className="text-right">Realized</th><th className="text-right">Unrealized</th><th className="text-right">Margin / Alloc</th><th>Last decision</th></tr></thead>
            <tbody>{strategies.data.map((s) => (
              <tr key={s.id} className="clickable" onClick={() => nav(`/strategies/${s.id}`)}>
                <td className="font-medium">{s.name}</td><td>{s.symbol}</td><td><StatusBadge status={s.status} /></td>
                <td><StatusBadge status={s.current_signal.signal} /></td>
                <td className="num"><Side side={s.position.side} /> {fmtQty(s.position.quantity)}</td>
                <td className={`num text-right font-semibold ${pnlClass(s.pnl?.today_pnl)}`}>{fmtSigned(s.pnl?.today_pnl)}</td>
                <td className={`num text-right ${pnlClass(s.pnl?.realized)}`}>{fmtSigned(s.pnl?.realized)}</td>
                <td className={`num text-right ${pnlClass(s.pnl?.unrealized)}`}>{fmtSigned(s.pnl?.unrealized)}</td>
                <td className="num text-right">{fmtINR(s.pnl?.margin_used)} / {fmtINR(s.allocation)}</td>
                <td className="max-w-[360px] truncate text-muted" title={s.last_signal?.reason}>{s.last_signal ? `${s.last_signal.signal} → ${s.last_signal.action}: ${s.last_signal.reason}` : 'no actionable signal yet'}</td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Panel>

      <div className="grid gap-3 xl:grid-cols-3">
        <Panel title="Recent orders" className="xl:col-span-2" right={<Link to="/orders" className="text-info text-[11px]">All orders →</Link>}>
          {orders.data ? <OrdersTable orders={orders.data} compact onSelect={() => nav('/orders')} /> : <Loading />}
        </Panel>
        <Panel title="Live event stream" right={<Link to="/audit" className="text-info text-[11px]">Audit trail →</Link>}>
          <EventFeed height="h-[340px]" />
        </Panel>
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <Panel title="Risk snapshot" right={<Link to="/risk" className="text-info text-[11px]">Risk →</Link>} bodyClass="p-3 space-y-3">
          {risk.data ? risk.data.portfolio.map((m: any) => <Meter key={m.label} m={m} />) : <Empty>Loading risk…</Empty>}
        </Panel>
        <Panel title="Demo & fault lab" bodyClass="p-3 text-xs space-y-2">
          <p className="text-muted">Run the scripted judge demo or inject faults (timeouts, mismatches, runaway strategy) into the real engine.</p>
          <div className="flex flex-wrap gap-2 text-[11px]">
            <span>Reconciliation: <StatusBadge status={overview.reconciliation.status} /></span>
          </div>
          <Link to="/faults" className="btn btn-primary">Open Fault Simulation &amp; Demo →</Link>
        </Panel>
      </div>
    </div>
  )
}
