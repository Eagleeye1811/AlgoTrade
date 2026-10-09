import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useSystem } from '../hooks/system'
import { useApi } from '../hooks/useEvents'
import { fmtINR, fmtSigned, pnlClass } from '../services/format'
import MarketChart from '../charts/MarketChart'
import EventFeed from '../components/EventFeed'
import Watchlist from '../components/Watchlist'
import OrdersTable from '../components/OrdersTable'
import { Empty, Loading, Meter, Panel, Stat, StatusBadge } from '../components/ui'

export default function Dashboard() {
  const { overview } = useSystem()
  const nav = useNavigate()
  const symbols = useApi<any[]>('/market/symbols')
  const strategies = useApi<any[]>('/strategies', ['strategy.update', 'strategy.status', 'strategy.config', 'position.update', 'strategy.signal'], 1500)
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

      <Panel title="Strategies" right={<Link to="/strategies" className="text-info text-[11px]">Manage →</Link>}>
        {!strategies.data ? <Loading /> : (
          <div className="overflow-x-auto"><table className="tbl">
            <thead><tr><th>Strategy</th><th>Stocks</th><th>Status</th><th>Last signal</th><th>Open positions</th><th className="text-right">Realized</th><th className="text-right">Unrealized</th><th className="text-right">Margin / Alloc</th><th>Last decision</th></tr></thead>
            <tbody>{strategies.data.map((s) => (
              <tr key={s.id} className="clickable" onClick={() => nav(`/strategies/${s.id}`)}>
                <td className="font-medium">{s.name}</td><td>{s.symbols.length}</td><td><StatusBadge status={s.status} /></td>
                <td>{s.last_signal ? <span className="flex items-center gap-1.5">{s.last_signal.symbol} <StatusBadge status={s.last_signal.signal} /></span> : <span className="text-muted">—</span>}</td>
                <td className="num">{s.open_positions}</td>
                <td className={`num text-right ${pnlClass(s.pnl?.realized)}`}>{fmtSigned(s.pnl?.realized)}</td>
                <td className={`num text-right ${pnlClass(s.pnl?.unrealized)}`}>{fmtSigned(s.pnl?.unrealized)}</td>
                <td className="num text-right">{fmtINR(s.pnl?.margin_used)} / {fmtINR(s.allocation)}</td>
                <td className="max-w-[360px] truncate text-muted" title={s.last_signal?.reason}>{s.last_signal ? `${s.last_signal.symbol} ${s.last_signal.signal} → ${s.last_signal.action}: ${s.last_signal.reason}` : 'no actionable signal yet'}</td>
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
