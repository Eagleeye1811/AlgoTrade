import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useApi, useEvents } from '../hooks/useEvents'
import { api } from '../services/api'
import { fmtINR, fmtNum, fmtQty, fmtSigned, fmtTime, pnlClass } from '../services/format'
import StrategyChart from '../charts/StrategyChart'
import OrdersTable from '../components/OrdersTable'
import StrategyConfigModal from '../components/StrategyConfigModal'
import { Empty, ErrorText, JsonKV, Loading, Panel, Side, Stat, StatusBadge } from '../components/ui'

export default function StrategyDetail() {
  const { id } = useParams()
  const nav = useNavigate()
  const detail = useApi<any>(`/strategies/${id}`, ['strategy.signal', 'strategy.status', 'strategy.config', 'order.update', 'position.update'], 1200)
  const chart = useApi<any>(`/strategies/${id}/chart?limit=300`, ['strategy.config'], 1000)
  const [live, setLive] = useState<any>(null)
  const [cfg, setCfg] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [pnl, setPnl] = useState<any>(null)
  useEvents(['strategy.update'], (e) => { if (e.strategy_id === id) { setLive(e.payload); chart.reload() } })
  useEvents(['pnl.update'], (e) => setPnl(e.payload.strategies?.[id!]))
  useEvents(['strategy.signal'], (e) => { if (e.strategy_id === id) chart.reload() })
  const d = detail.data
  if (!d) return detail.error ? <ErrorText error={detail.error} /> : <Loading />
  const sig = live ? { signal: live.signal, reason: live.reason, indicators: live.indicators } : d.current_signal
  const p = pnl || d.pnl || {}
  const toggle = async () => {
    setErr(null)
    try { await api.post(`/strategies/${d.id}/${d.status === 'RUNNING' ? 'pause' : 'resume'}`); detail.reload() } catch (e: any) { setErr(e.message) }
  }
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-base font-semibold">{d.name}</h1>
        <span className="text-muted">{d.symbol} · {d.timeframe} · config v{d.config_version} · {d.id}</span>
        <StatusBadge status={d.status} />{d.status_reason && <span className="text-xs text-amber-400">{d.status_reason}</span>}
        <div className="ml-auto flex gap-2">
          <button className="btn" onClick={toggle}>{d.status === 'RUNNING' ? 'Pause' : 'Resume'}</button>
          <button className="btn" onClick={() => setCfg(true)}>Configure</button>
        </div>
      </div>
      <ErrorText error={err} />
      <p className="text-xs text-muted">{d.description}</p>
      <Panel bodyClass="grid grid-cols-2 sm:grid-cols-5 xl:grid-cols-10 divide-x divide-line">
        <Stat label="Current price" value={fmtNum(d.ltp)} />
        <Stat label="Signal" value={<StatusBadge status={sig.signal} />} />
        <Stat label="Position" value={<><Side side={d.position.side} /> {fmtQty(d.position.quantity)}</>} sub={Number(d.position.quantity) ? `@ ${fmtNum(d.position.avg_price)}` : undefined} />
        <Stat label="Stop-loss" value={fmtNum(d.exits?.stop_loss)} className={d.exits?.stop_loss ? 'text-neg' : ''}
          sub={d.params.stop_loss_pct ? `${d.params.stop_loss_pct}% from entry` : 'off'} />
        <Stat label="Take-profit" value={fmtNum(d.exits?.take_profit)} className={d.exits?.take_profit ? 'text-pos' : ''}
          sub={d.params.take_profit_pct ? `${d.params.take_profit_pct}% from entry` : 'off'} />
        <Stat label="P&L today" value={fmtSigned(p.today_pnl)} className={pnlClass(p.today_pnl)}
          sub={d.limits?.max_daily_loss ? `limit −${fmtINR(d.limits.max_daily_loss)}` : undefined} />
        <Stat label="Realized" value={fmtSigned(p.realized)} className={pnlClass(p.realized)} />
        <Stat label="Unrealized" value={fmtSigned(p.unrealized)} className={pnlClass(p.unrealized)} />
        <Stat label="Margin used" value={fmtINR(p.margin_used)} sub={`of ${fmtINR(d.allocation)}`} />
        <Stat label="Order size" value={`${fmtQty(d.quantity)} sh`} />
      </Panel>
      <div className="grid gap-3 xl:grid-cols-3">
        <Panel title={`Chart · ${d.symbol} ${d.timeframe}`} className="xl:col-span-2">
          {chart.data ? <StrategyChart data={chart.data} /> : <Loading />}
        </Panel>
        <div className="space-y-3">
          <Panel title="Live indicators" bodyClass="p-3 space-y-2 text-xs">
            <div className="text-muted">{sig.reason}</div>
            <JsonKV data={sig.indicators} />
          </Panel>
          <Panel title="Last actionable signal" bodyClass="p-3 text-xs space-y-1">
            {d.last_signal ? <>
              <div className="flex items-center gap-2"><StatusBadge status={d.last_signal.signal} /><span className="text-muted">{d.last_signal.action}</span><span className="num">@ {fmtNum(d.last_signal.price)}</span></div>
              <div>{d.last_signal.reason}</div>
              <Link className="text-info" to={`/audit?trace=${d.last_signal.correlation_id}`}>Trace this decision →</Link>
            </> : <Empty>No actionable signal since start</Empty>}
          </Panel>
          <Panel title="Limits" bodyClass="p-3"><JsonKV data={d.limits} /></Panel>
        </div>
      </div>
      <Panel title="Recent signals">
        {d.signals.length === 0 ? <Empty>No BUY/SELL signals yet</Empty> : (
          <div className="overflow-x-auto"><table className="tbl"><thead><tr><th>Time</th><th>Signal</th><th>Price</th><th>Action</th><th>Origin</th><th>Reason</th><th></th></tr></thead>
            <tbody>{d.signals.map((x: any) => (
              <tr key={x.id}><td className="num text-muted">{fmtTime(x.created_at)}</td><td><StatusBadge status={x.signal} /></td><td className="num">{fmtNum(x.price)}</td>
                <td><span className={x.action.startsWith('ORDER') ? 'text-blue-300' : 'text-muted'}>{x.action}</span></td><td className="text-muted">{x.origin}</td>
                <td className="max-w-[420px] truncate" title={x.reason}>{x.reason}</td><td><Link className="text-info" to={`/audit?trace=${x.correlation_id}`}>trace</Link></td></tr>))}</tbody></table></div>
        )}
      </Panel>
      <Panel title="Recent orders"><OrdersTable orders={d.orders} onSelect={() => nav('/orders')} /></Panel>
      <Panel title="Configuration history">
        <table className="tbl"><thead><tr><th>Version</th><th>Parameters</th><th>Active</th><th>Created</th></tr></thead>
          <tbody>{d.config_history.map((c: any) => <tr key={c.version}><td>v{c.version}</td><td><JsonKV data={c.params} /></td><td>{c.active ? <StatusBadge status="RUNNING" label="ACTIVE" /> : ''}</td><td className="num text-muted">{fmtTime(c.created_at, true)}</td></tr>)}</tbody></table>
      </Panel>
      <StrategyConfigModal strategy={d} open={cfg} onClose={() => setCfg(false)} onSaved={() => { detail.reload(); chart.reload() }} />
    </div>
  )
}
