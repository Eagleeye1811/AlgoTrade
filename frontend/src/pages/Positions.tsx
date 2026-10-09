import { useApi, useEvents } from '../hooks/useEvents'
import { fmtINR, fmtNum, fmtQty, fmtSigned, fmtTime, pnlClass } from '../services/format'
import LineChart from '../charts/LineChart'
import { Badge, Empty, Loading, Panel, Side, Stat } from '../components/ui'

export default function Positions() {
  const pos = useApi<any>('/positions')
  useEvents(['pnl.update'], (e) => pos.setData((d: any) => ({ ...(d || {}), ...e.payload })))
  const hist = useApi<any[]>('/pnl/history?limit=720', [], 0)
  const events = useApi<any[]>('/positions/events?limit=100', ['position.update', 'reconciliation.completed'], 1000)
  if (!pos.data) return <Loading />
  const p = pos.data.portfolio
  const strategies = Object.values(pos.data.strategies || {}) as any[]
  return (
    <div className="space-y-3">
      <h1 className="text-base font-semibold">Positions &amp; P&amp;L</h1>
      <Panel bodyClass="grid grid-cols-2 sm:grid-cols-4 xl:grid-cols-8 divide-x divide-line">
        <Stat label="Equity" value={fmtINR(p.equity)} />
        <Stat label="Total P&L" value={fmtSigned(p.total_pnl)} className={pnlClass(p.total_pnl)} />
        <Stat label="Realized" value={fmtSigned(p.realized)} className={pnlClass(p.realized)} />
        <Stat label="Unrealized" value={fmtSigned(p.unrealized)} className={pnlClass(p.unrealized)} />
        <Stat label="Today's P&L" value={fmtSigned(p.today_pnl)} className={pnlClass(p.today_pnl)} />
        <Stat label="Exposure" value={fmtINR(p.exposure)} />
        <Stat label="Margin used" value={fmtINR(p.margin_used)} />
        <Stat label="Available" value={fmtINR(p.available_capital)} />
      </Panel>
      <Panel title="Open positions (from actual fills, marked to LTP)">
        {pos.data.positions.length === 0 ? <Empty>No positions yet</Empty> : (
          <div className="overflow-x-auto"><table className="tbl"><thead><tr><th>Strategy</th><th>Symbol</th><th>Side</th><th className="text-right">Qty</th><th className="text-right">Avg entry</th><th className="text-right">LTP</th><th className="text-right">Realized</th><th className="text-right">Unrealized</th><th className="text-right">Exposure</th><th className="text-right">Margin</th></tr></thead>
            <tbody>{pos.data.positions.map((x: any) => (
              <tr key={x.strategy_id + x.symbol}><td>{x.strategy_id}</td><td>{x.symbol}</td><td><Side side={x.side} /></td><td className="num text-right">{fmtQty(x.quantity)}</td>
                <td className="num text-right">{x.quantity ? fmtNum(x.avg_price) : '—'}</td><td className="num text-right">{fmtNum(x.ltp)}</td>
                <td className={`num text-right ${pnlClass(x.realized)}`}>{fmtSigned(x.realized, 2)}</td><td className={`num text-right ${pnlClass(x.unrealized)}`}>{fmtSigned(x.unrealized, 2)}</td>
                <td className="num text-right">{fmtINR(x.exposure)}</td><td className="num text-right">{fmtINR(x.margin)}</td></tr>))}</tbody></table></div>
        )}
      </Panel>
      <Panel title="Per-strategy breakdown (isolated books)">
        <div className="overflow-x-auto"><table className="tbl"><thead><tr><th>Strategy</th><th className="text-right">Allocation</th><th className="text-right">Margin used</th><th className="text-right">Reserved</th><th className="text-right">Available</th><th className="text-right">Exposure</th><th className="text-right">Realized</th><th className="text-right">Unrealized</th><th className="text-right">Total</th></tr></thead>
          <tbody>{strategies.map((s) => (
            <tr key={s.strategy_id}><td>{s.name}</td><td className="num text-right">{fmtINR(s.allocation)}</td><td className="num text-right">{fmtINR(s.margin_used)}</td><td className="num text-right">{fmtINR(s.reserved_margin)}</td>
              <td className="num text-right">{fmtINR(s.available_margin)}</td><td className="num text-right">{fmtINR(s.exposure)}</td>
              <td className={`num text-right ${pnlClass(s.realized)}`}>{fmtSigned(s.realized, 2)}</td><td className={`num text-right ${pnlClass(s.unrealized)}`}>{fmtSigned(s.unrealized, 2)}</td>
              <td className={`num text-right ${pnlClass(s.total_pnl)}`}>{fmtSigned(s.total_pnl, 2)}</td></tr>))}</tbody></table></div>
      </Panel>
      <Panel title="Portfolio P&L history (persisted snapshots)" right={<button className="btn" onClick={hist.reload}>Refresh</button>}>
        {!hist.data ? <Loading /> : hist.data.length < 2 ? <Empty>Not enough snapshots yet</Empty> :
          <LineChart rows={hist.data} lines={[{ key: 'total', title: 'Total', color: '#38bdf8' }, { key: 'realized', title: 'Realized', color: '#22c55e' }]} />}
      </Panel>
      <Panel title="Position events">
        {!events.data ? <Loading /> : events.data.length === 0 ? <Empty>No position changes yet</Empty> : (
          <div className="overflow-x-auto"><table className="tbl"><thead><tr><th>Time</th><th>Strategy</th><th>Symbol</th><th>Event</th><th className="text-right">Qty</th><th className="text-right">Avg</th><th className="text-right">Realized Δ</th><th>Detail</th></tr></thead>
            <tbody>{events.data.map((e) => (
              <tr key={e.id}><td className="num text-muted">{fmtTime(e.created_at)}</td><td>{e.strategy_id}</td><td>{e.symbol}</td>
                <td>{e.event_type === 'FILL' ? <Badge tone="blue">FILL</Badge> : <Badge tone="amber">{e.event_type}</Badge>}</td>
                <td className="num text-right">{fmtQty(e.qty_before)} → {fmtQty(e.qty_after)}</td><td className="num text-right">{fmtNum(e.avg_after)}</td>
                <td className={`num text-right ${pnlClass(e.realized_delta)}`}>{fmtSigned(e.realized_delta, 2)}</td>
                <td className="text-muted">{e.data?.reason || (e.data?.fill_qty ? `${e.data.side} ${e.data.fill_qty} @ ${e.data.fill_price}` : '')}</td></tr>))}</tbody></table></div>
        )}
      </Panel>
    </div>
  )
}
