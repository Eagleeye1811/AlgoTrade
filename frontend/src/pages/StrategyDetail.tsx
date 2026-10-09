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
  const [sel, setSel] = useState<string | null>(null)
  const chart = useApi<any>(`/strategies/${id}/chart?limit=300${sel ? `&symbol=${sel}` : ''}`, ['strategy.config'], 1000)
  const [live, setLive] = useState<any>(null)
  const [cfg, setCfg] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [pnl, setPnl] = useState<any>(null)
  const d = detail.data
  const symbol: string | undefined = sel ?? d?.symbol
  useEvents(['strategy.update'], (e) => { if (e.strategy_id === id && e.symbol === symbol) { setLive(e.payload); chart.reload() } })
  useEvents(['pnl.update'], (e) => setPnl(e.payload.strategies?.[id!]))
  useEvents(['strategy.signal'], (e) => { if (e.strategy_id === id && e.symbol === symbol) chart.reload() })
  if (!d) return detail.error ? <ErrorText error={detail.error} /> : <Loading />
  const book = d.books.find((b: any) => b.symbol === symbol) ?? d.books[0]
  const sig = live ? { signal: live.signal, reason: live.reason, indicators: live.indicators }
    : symbol === d.symbol ? d.current_signal : { signal: book.signal, reason: book.reason, indicators: {} }
  const pick = (x: string) => { setLive(null); setSel(x) }
  const p = pnl || d.pnl || {}
  const toggle = async () => {
    setErr(null)
    try { await api.post(`/strategies/${d.id}/${d.status === 'RUNNING' ? 'pause' : 'resume'}`); detail.reload() } catch (e: any) { setErr(e.message) }
  }
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-base font-semibold">{d.name}</h1>
        <span className="text-muted">{d.symbols.length} stocks · {d.timeframe} · config v{d.config_version}</span>
        <StatusBadge status={d.status} />{d.status_reason && <span className="text-xs text-amber-400">{d.status_reason}</span>}
        <div className="ml-auto flex gap-2">
          <button className="btn" onClick={toggle}>{d.status === 'RUNNING' ? 'Pause' : 'Resume'}</button>
          <button className="btn" onClick={() => setCfg(true)}>Configure</button>
        </div>
      </div>
      <ErrorText error={err} />
      <p className="text-xs text-muted">{d.description}</p>
      <Panel bodyClass="grid grid-cols-2 sm:grid-cols-5 xl:grid-cols-10 divide-x divide-line">
        <Stat label="Open positions" value={`${d.open_positions} / ${d.symbols.length}`} />
        <Stat label="Stop-loss" value={d.params.stop_loss_pct ? `${d.params.stop_loss_pct}%` : 'off'} sub="from entry" />
        <Stat label="Take-profit" value={d.params.take_profit_pct ? `${d.params.take_profit_pct}%` : 'off'} sub="from entry" />
        <Stat label="Max order size" value={`${fmtQty(d.quantity)} sh`} sub="capped by budget per stock" />
        <Stat label="Budget per stock" value={fmtINR(d.allocation / Math.max(1, d.symbols.length))} />
        <Stat label="Realized" value={fmtSigned(p.realized)} className={pnlClass(p.realized)} />
        <Stat label="Unrealized" value={fmtSigned(p.unrealized)} className={pnlClass(p.unrealized)} />
        <Stat label="Margin used" value={fmtINR(p.margin_used)} sub={`of ${fmtINR(d.allocation)}`} />
        <Stat label="Exposure" value={fmtINR(p.exposure)} />
      </Panel>
      <Panel title="Stocks" right={<span className="text-muted text-[11px]">click a row to chart it</span>}>
        <div className="overflow-x-auto"><table className="tbl">
          <thead><tr><th>Symbol</th><th>Signal</th><th>Position</th><th className="text-right">Avg entry</th><th className="text-right">LTP</th>
            <th className="text-right">Unrealized</th><th className="text-right">Stop-loss</th><th className="text-right">Take-profit</th><th>Latest evaluation</th></tr></thead>
          <tbody>{d.books.map((b: any) => {
            const q = Number(b.position.quantity), u = q && b.ltp != null ? (Number(b.ltp) - Number(b.position.avg_price)) * q : null
            return (
              <tr key={b.symbol} className={`clickable ${b.symbol === symbol ? 'bg-panel2' : ''}`} onClick={() => pick(b.symbol)}>
                <td className="font-medium">{b.symbol}{b.symbol === symbol && <span className="ml-1.5 text-info">●</span>}</td>
                <td><StatusBadge status={b.signal} /></td>
                <td className="num">{q ? <><Side side={b.position.side} /> {fmtQty(q)}</> : <span className="text-muted">flat</span>}</td>
                <td className="num text-right">{q ? fmtNum(b.position.avg_price) : '—'}</td>
                <td className="num text-right">{fmtNum(b.ltp)}</td>
                <td className={`num text-right ${pnlClass(u)}`}>{u === null ? '—' : fmtSigned(u, 2)}</td>
                <td className="num text-right text-neg">{fmtNum(b.exits?.stop_loss)}</td>
                <td className="num text-right text-pos">{fmtNum(b.exits?.take_profit)}</td>
                <td className="max-w-[380px] truncate text-muted" title={b.reason}>{b.reason}</td>
              </tr>)
          })}</tbody>
        </table></div>
      </Panel>
      <div className="grid gap-3 xl:grid-cols-3">
        <Panel title={`Chart · ${symbol} ${d.timeframe}`} className="xl:col-span-2"
          right={<select className="input py-0.5 text-[11px]" value={symbol} onChange={(e) => pick(e.target.value)}>
            {d.symbols.map((x: string) => <option key={x}>{x}</option>)}</select>}>
          {chart.data ? <StrategyChart data={chart.data} /> : <Loading />}
        </Panel>
        <div className="space-y-3">
          <Panel title={`Live indicators · ${symbol}`} bodyClass="p-3 space-y-2 text-xs">
            <div className="text-muted">{sig.reason}</div>
            <JsonKV data={sig.indicators} />
          </Panel>
          <Panel title="Last actionable signal" bodyClass="p-3 text-xs space-y-1">
            {d.last_signal ? <>
              <div className="flex items-center gap-2"><span className="font-medium">{d.last_signal.symbol}</span><StatusBadge status={d.last_signal.signal} /><span className="text-muted">{d.last_signal.action}</span><span className="num">@ {fmtNum(d.last_signal.price)}</span></div>
              <div>{d.last_signal.reason}</div>
              <Link className="text-info" to={`/audit?trace=${d.last_signal.correlation_id}`}>Trace this decision →</Link>
            </> : <Empty>No actionable signal since start</Empty>}
          </Panel>
          <Panel title="Limits" bodyClass="p-3"><JsonKV data={d.limits} /></Panel>
        </div>
      </div>
      <Panel title="Recent signals">
        {d.signals.length === 0 ? <Empty>No BUY/SELL signals yet</Empty> : (
          <div className="overflow-x-auto"><table className="tbl"><thead><tr><th>Time</th><th>Symbol</th><th>Signal</th><th>Price</th><th>Action</th><th>Origin</th><th>Reason</th><th></th></tr></thead>
            <tbody>{d.signals.map((x: any) => (
              <tr key={x.id}><td className="num text-muted">{fmtTime(x.created_at)}</td><td className="font-medium">{x.symbol}</td><td><StatusBadge status={x.signal} /></td><td className="num">{fmtNum(x.price)}</td>
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
