import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useApi } from '../hooks/useEvents'
import { api } from '../services/api'
import { fmtNum, fmtQty, fmtTime, shortId } from '../services/format'
import OrdersTable from '../components/OrdersTable'
import RiskChecks from '../components/RiskChecks'
import { Badge, Drawer, Empty, ErrorText, JsonKV, Loading, Panel, Side, StatusBadge } from '../components/ui'

const STATUSES = ['', 'CREATED', 'RISK_CHECK', 'APPROVED', 'SUBMITTED', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'FILLED', 'REJECTED', 'CANCELLED', 'TIMEOUT']

export default function Orders() {
  const [tab, setTab] = useState<'orders' | 'fills'>('orders')
  const [status, setStatus] = useState('')
  const [strategy, setStrategy] = useState('')
  const [sel, setSel] = useState<string | null>(null)
  const q = new URLSearchParams({ limit: '300', ...(status ? { status } : {}), ...(strategy ? { strategy_id: strategy } : {}) })
  const orders = useApi<any[]>(`/orders?${q}`, ['order.update'], 700)
  const fills = useApi<any[]>(tab === 'fills' ? '/fills?limit=300' : null, ['fill.received'], 700)
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="mr-3 text-base font-semibold">Orders &amp; Execution</h1>
        {(['orders', 'fills'] as const).map((t) => <button key={t} className={`btn ${tab === t ? 'btn-primary' : ''}`} onClick={() => setTab(t)}>{t === 'orders' ? 'Orders' : 'Fills'}</button>)}
        {tab === 'orders' && <>
          <select className="input ml-auto" value={status} onChange={(e) => setStatus(e.target.value)}>{STATUSES.map((s) => <option key={s} value={s}>{s || 'All statuses'}</option>)}</select>
          <select className="input" value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            <option value="">All strategies</option><option value="rsi_ema">RSI + EMA</option><option value="macd">MACD</option><option value="breakout">Breakout</option></select>
        </>}
      </div>
      {tab === 'orders' ? (
        <Panel title={`Orders (${orders.data?.length ?? 0})`}>{orders.data ? <OrdersTable orders={orders.data} onSelect={(o) => setSel(o.id)} /> : <Loading />}</Panel>
      ) : (
        <Panel title="Fills (actual executions)">
          {!fills.data ? <Loading /> : fills.data.length === 0 ? <Empty>No fills yet</Empty> : (
            <div className="overflow-x-auto"><table className="tbl"><thead><tr><th>Time</th><th>Fill</th><th>Order</th><th>Strategy</th><th>Symbol</th><th>Side</th><th className="text-right">Qty</th><th className="text-right">Price</th><th className="text-right">Ref price</th><th className="text-right">Slippage</th><th className="text-right">Latency</th><th>Source</th></tr></thead>
              <tbody>{fills.data.map((f) => (
                <tr key={f.id} className="clickable" onClick={() => setSel(f.order_id)}><td className="num text-muted">{fmtTime(f.filled_at)}</td><td className="num">{f.broker_fill_id}</td><td className="num">{shortId(f.order_id)}</td>
                  <td>{f.strategy_id}</td><td>{f.symbol}</td><td><Side side={f.side} /></td><td className="num text-right">{fmtQty(f.quantity)}</td>
                  <td className="num text-right">{fmtNum(f.price)}</td><td className="num text-right">{fmtNum(f.reference_price)}</td>
                  <td className="num text-right">{fmtNum(f.slippage_bps, 1)} bps</td><td className="num text-right">{f.latency_ms} ms</td>
                  <td>{f.source === 'RECONCILIATION' ? <Badge tone="amber">recon</Badge> : <span className="text-muted">report</span>}</td></tr>))}</tbody></table></div>
          )}
        </Panel>
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
