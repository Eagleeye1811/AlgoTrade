import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useApi } from '../hooks/useEvents'
import { api } from '../services/api'
import { fmtNum, fmtQty, fmtTime, shortId } from '../services/format'
import OrdersTable from '../components/OrdersTable'
import TradesSummary, { TradeList, downloadTradesCsv, useTradeFilter } from '../components/TradesSummary'
import { Download } from 'lucide-react'
import RiskChecks from '../components/RiskChecks'
import { Drawer, Empty, ErrorText, JsonKV, Loading, Panel, Side, StatusBadge } from '../components/ui'

/** Download rows as CSV (what the table currently shows). */
function downloadCsv(name: string, rows: any[], cols: string[]) {
  const cell = (v: any) => { const s = v == null ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s }
  const text = [cols.join(','), ...rows.map((r) => cols.map((c) => cell(r[c])).join(','))].join('\n')
  const url = URL.createObjectURL(new Blob([text], { type: 'text/csv' }))
  const a = document.createElement('a'); a.href = url; a.download = name; a.click(); URL.revokeObjectURL(url)
}
const ORDER_COLS = ['created_at', 'id', 'broker_order_id', 'strategy_id', 'symbol', 'side', 'order_type', 'quantity', 'filled_quantity',
  'avg_fill_price', 'limit_price', 'status', 'origin', 'reduce_only', 'reject_rule', 'reject_reason', 'latency_ms', 'correlation_id']

// quick status filters (client-side over the latest 500 orders), with live counts
const OPEN = ['CREATED', 'RISK_CHECK', 'APPROVED', 'SUBMITTED', 'ACKNOWLEDGED', 'TIMEOUT']
const STATUS_FILTERS: { key: string; label: string; test: (s: string) => boolean }[] = [
  { key: 'ALL', label: 'All', test: () => true },
  { key: 'OPEN', label: 'Open', test: (s) => OPEN.includes(s) },
  { key: 'FILLED', label: 'Filled', test: (s) => s === 'FILLED' },
  { key: 'PARTIALLY_FILLED', label: 'Partially filled', test: (s) => s === 'PARTIALLY_FILLED' },
  { key: 'REJECTED', label: 'Rejected', test: (s) => s === 'REJECTED' },
  { key: 'CANCELLED', label: 'Cancelled', test: (s) => s === 'CANCELLED' || s === 'EXPIRED' },
]
const ORIGINS: Record<string, string> = { STRATEGY: 'Strategy signal', STOP_LOSS: 'Stop-loss', TAKE_PROFIT: 'Target', SQUARE_OFF: 'Square-off',
  KILL_SWITCH: 'Kill switch', CIRCUIT_BREAKER: 'Halt & flatten', FAULT_LAB: 'Fault Lab', DEMO: 'Demo', RUNAWAY: 'Runaway (simulated)' }

export default function Orders() {
  const [tab, setTab] = useState<'orders' | 'trades'>('orders')
  const [status, setStatus] = useState('ALL')
  const [side, setSide] = useState('')
  const [origin, setOrigin] = useState('')
  const [strategy, setStrategy] = useState('')
  const [sel, setSel] = useState<string | null>(null)
  const f = useTradeFilter()
  const q = new URLSearchParams({ limit: '500', ...(strategy ? { strategy_id: strategy } : {}) })
  const orders = useApi<any[]>(`/orders?${q}`, ['order.update'], 700)
  const base = (orders.data ?? []).filter((o) => (!side || o.side === side) && (!origin || o.origin === origin))
  const shown = base.filter((o) => STATUS_FILTERS.find((x) => x.key === status)!.test(o.status))
  const subs = useApi<any[]>('/strategies', ['strategy.update', 'strategy.status'])
  const stamp = new Date(Date.now() + 19800000).toISOString().slice(0, 16).replace(/[-:T]/g, '')
  const csv = () => tab === 'orders' ? downloadCsv(`orders-${status.toLowerCase()}-${stamp}.csv`, shown, ORDER_COLS)
    : downloadTradesCsv(f)
  return (
    <div className="space-y-3">
      <TradesSummary f={f} title="Orders, Trades & P&L" />
      <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3">
        {(['orders', 'trades'] as const).map((t) => <button key={t} className={`btn ${tab === t ? 'btn-primary' : ''}`} onClick={() => setTab(t)}>
          {t === 'orders' ? 'Orders' : 'Trades (net of charges)'}</button>)}
        {tab === 'orders' && <>
          <select className="input ml-auto" value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            <option value="">All strategies</option>
            {(subs.data ?? []).map((s: any) => <option key={s.id} value={s.id}>{s.name} · {s.symbol} · {s.id}</option>)}</select>
          <select className="input" value={side} onChange={(e) => setSide(e.target.value)}>
            <option value="">Buy &amp; sell</option><option value="BUY">Buy</option><option value="SELL">Sell</option></select>
          <select className="input" value={origin} onChange={(e) => setOrigin(e.target.value)}>
            <option value="">All origins</option>{Object.entries(ORIGINS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        </>}
        <button className={`btn ${tab !== 'orders' ? 'ml-auto' : ''}`} onClick={csv}><Download size={14} /> CSV</button>
      </div>
      {tab === 'orders' && (
        <div className="flex flex-wrap gap-1.5">
          {STATUS_FILTERS.map((x) => {
            const n = base.filter((o) => x.test(o.status)).length
            const tone = x.key === 'REJECTED' ? 'text-neg' : x.key === 'PARTIALLY_FILLED' ? 'text-amber-300' : x.key === 'FILLED' ? 'text-pos' : ''
            return (
              <button key={x.key} onClick={() => setStatus(x.key)}
                className={`rounded-full border px-3 py-1 text-xs font-medium ${status === x.key ? 'border-info bg-info/20 text-white' : 'border-line text-muted hover:text-text'}`}>
                {x.label} <span className={`num ml-1 ${status === x.key ? '' : tone}`}>{n}</span>
              </button>
            )
          })}
        </div>
      )}
      {tab === 'orders' ? (
        <Panel title={`Orders · ${STATUS_FILTERS.find((x) => x.key === status)!.label} (${shown.length})`}>
          {orders.data ? shown.length ? <OrdersTable orders={shown} onSelect={(o) => setSel(o.id)} /> : <Empty>No orders match these filters</Empty> : <Loading />}</Panel>
      ) : (
        <TradeList f={f} />
      )}
      <OrderDrawer id={sel} onClose={() => setSel(null)} onChanged={orders.reload} />
    </div>
  )
}

export function OrderDrawer({ id, onClose, onChanged }: { id: string | null; onClose: () => void; onChanged?: () => void }) {
  const d = useApi<any>(id ? `/orders/${id}` : null, ['order.update', 'order.event', 'fill.received'], 600)
  const [err, setErr] = useState<string | null>(null)
  const o = d.data?.order
  const open = o && ['APPROVED', 'SUBMITTED', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'CREATED'].includes(o.status)
  const cancel = async () => { setErr(null); try { await api.post(`/orders/${id}/cancel`); d.reload(); onChanged?.() } catch (e: any) { setErr(e.message) } }
  const failed = d.data?.risk?.flatMap((r: any) => r.checks.filter((c: any) => c.result === 'FAIL')) ?? []
  return (
    <Drawer open={!!id} onClose={onClose} title={`Order ${shortId(id)}`}>
      {!d.data || d.data.order.id !== id ? <Loading /> : <>
        <div className="flex flex-wrap items-center gap-3">
          <Side side={o.side} /><span className="num text-lg">{fmtQty(o.quantity)} {o.symbol}</span><StatusBadge status={o.status} />
          <span className="text-muted">{o.order_type}{o.limit_price ? ` @ ${fmtNum(o.limit_price)}` : ''} · {o.strategy_id} · {o.origin}{o.reduce_only ? ' · reduce-only' : ''}</span>
          <div className="ml-auto flex gap-2">
            {open && <button className="btn btn-danger" onClick={cancel}>Cancel order</button>}
            <Link className="btn" to={`/audit?trace=${o.correlation_id}`} onClick={onClose}>Trace</Link>
          </div>
        </div>
        <ErrorText error={err} />
        {o.status === 'REJECTED' && (
          <div className="rounded border border-red-500/40 bg-red-500/10 p-3">
            <div className="font-bold text-red-300">ORDER REJECTED{failed[0] ? ` — ${failed[0].label}` : ''}</div>
            {failed.map((c: any, i: number) => <div key={i} className="num text-xs text-red-200">{c.message} · Requested/actual: {c.actual} · Allowed: {c.allowed}</div>)}
            {!failed.length && <div className="text-xs text-red-200">{o.reject_reason}</div>}
          </div>
        )}
        <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
          <div><div className="text-muted">Filled</div><div className="num">{fmtQty(o.filled_quantity)} / {fmtQty(o.quantity)}</div></div>
          <div><div className="text-muted">Avg fill</div><div className="num">{fmtNum(o.avg_fill_price)}</div></div>
          <div><div className="text-muted">Ack latency</div><div className="num">{o.latency_ms != null ? `${o.latency_ms} ms` : '—'}</div></div>
          <div><div className="text-muted">Broker order</div><div className="num">{o.broker_order_id || '—'}</div></div>
          <div className="col-span-2"><div className="text-muted">Idempotency key</div><div className="num break-all">{o.client_order_id}</div></div>
          <div className="col-span-2"><div className="text-muted">Correlation</div><div className="num break-all">{o.correlation_id}</div></div>
        </div>
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-wider text-muted">Lifecycle</div>
          <ol className="relative ml-2 border-l border-line">
            {d.data.lifecycle.map((e: any, i: number) => (
              <li key={i} className="mb-2 ml-4">
                <span className={`absolute -left-[5px] mt-1 h-2.5 w-2.5 rounded-full ${['REJECTED', 'UNCERTAIN', 'VERIFY_FAILED'].includes(e.status) ? 'bg-red-500' : ['TIMEOUT', 'NOT_FOUND_AT_BROKER', 'PARTIALLY_FILLED'].includes(e.status) ? 'bg-amber-500' : e.status === 'FILLED' ? 'bg-green-500' : 'bg-blue-500'}`} />
                <div className="flex items-center gap-2"><StatusBadge status={e.status} /><span className="num text-[11px] text-muted">{fmtTime(e.at)}.{String(new Date(e.at).getMilliseconds()).padStart(3, '0')}</span></div>
                <div className="text-xs">{e.message}</div>
              </li>))}
          </ol>
        </div>
        {d.data.risk.map((r: any) => (
          <div key={r.id}><div className="mb-1 flex items-center gap-2 text-[11px] uppercase tracking-wider text-muted">Risk firewall <StatusBadge status={r.decision} /></div><RiskChecks checks={r.checks} /></div>
        ))}
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-wider text-muted">Fills</div>
          {d.data.fills.length === 0 ? <Empty>No fills</Empty> : <table className="tbl"><thead><tr><th>Time</th><th>Qty</th><th>Price</th><th>Ref</th><th>Slippage</th><th>Latency</th><th>Source</th></tr></thead>
            <tbody>{d.data.fills.map((f: any) => <tr key={f.id}><td className="num text-muted">{fmtTime(f.filled_at)}</td><td className="num">{fmtQty(f.quantity)}</td><td className="num">{fmtNum(f.price)}</td><td className="num">{fmtNum(f.reference_price)}</td><td className="num">{fmtNum(f.slippage_bps, 1)} bps</td><td className="num">{f.latency_ms} ms</td><td>{f.source}</td></tr>)}</tbody></table>}
        </div>
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-wider text-muted">Originating signal</div>
          {d.data.signal ? <div className="space-y-1 text-xs"><div className="flex gap-2"><StatusBadge status={d.data.signal.signal} /><span>{d.data.signal.reason}</span></div><JsonKV data={d.data.signal.indicators} /></div> : <Empty>No signal recorded (kill-switch / manual order)</Empty>}
        </div>
      </>}
    </Drawer>
  )
}
