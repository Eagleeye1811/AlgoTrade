import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useApi } from '../hooks/useEvents'
import { fmtNum, fmtQty, fmtSigned, fmtTime, pnlClass, shortId } from '../services/format'
import RiskChecks from '../components/RiskChecks'
import { Badge, Empty, ErrorText, JsonKV, Loading, Panel, Side, StatusBadge } from '../components/ui'

const SEV: Record<string, 'red' | 'amber' | 'blue'> = { CRITICAL: 'red', ERROR: 'red', WARN: 'amber', INFO: 'blue' }

export default function Audit() {
  const [params, setParams] = useSearchParams()
  const trace = params.get('trace')
  return trace ? <Trace id={trace} onBack={() => setParams({})} /> : <AuditList onTrace={(id) => setParams({ trace: id })} />
}

function AuditList({ onTrace }: { onTrace: (id: string) => void }) {
  const [type, setType] = useState('')
  const [sev, setSev] = useState('')
  const [q, setQ] = useState('')
  const [query, setQuery] = useState('')
  const qs = new URLSearchParams({ limit: '300', ...(type ? { event_type: type } : {}), ...(sev ? { severity: sev } : {}), ...(query ? { q: query } : {}) })
  const list = useApi<any[]>(`/audit?${qs}`, ['audit.event'], 1500)
  const types = Array.from(new Set((list.data || []).map((e) => e.event_type))).sort()
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="mr-3 text-base font-semibold">Audit trail</h1>
        <select className="input" value={type} onChange={(e) => setType(e.target.value)}><option value="">All event types</option>{types.map((t) => <option key={t}>{t}</option>)}{type && !types.includes(type) && <option>{type}</option>}</select>
        <select className="input" value={sev} onChange={(e) => setSev(e.target.value)}><option value="">All severities</option>{['INFO', 'WARN', 'ERROR', 'CRITICAL'].map((s) => <option key={s}>{s}</option>)}</select>
        <form onSubmit={(e) => { e.preventDefault(); setQuery(q) }} className="flex gap-1"><input className="input w-64" placeholder="Search text / correlation / order id" value={q} onChange={(e) => setQ(e.target.value)} /><button className="btn">Search</button></form>
      </div>
      <Panel>
        {!list.data ? <Loading /> : list.data.length === 0 ? <Empty>No audit events match</Empty> : (
          <div className="overflow-x-auto"><table className="tbl"><thead><tr><th>Time</th><th>Severity</th><th>Type</th><th>Source</th><th>Strategy</th><th>Message</th><th>Actor</th><th>Trace</th></tr></thead>
            <tbody>{list.data.map((e) => (
              <tr key={e.id}><td className="num text-muted">{fmtTime(e.created_at, true)}</td><td><Badge tone={SEV[e.severity] ?? 'gray'}>{e.severity}</Badge></td>
                <td className="font-mono text-[11px]">{e.event_type}</td><td className="text-muted">{e.source}</td>
                <td className="whitespace-nowrap">{e.strategy_id ? <Link className="text-info" to={`/strategies/${e.strategy_id}`}>{e.strategy_id}</Link> : <span className="text-muted">—</span>}</td>
                <td className="max-w-[560px] whitespace-normal">{e.message}</td><td className="text-muted">{e.actor || ''}</td>
                <td>{e.correlation_id && <button className="text-info" onClick={() => onTrace(e.correlation_id)}>{shortId(e.correlation_id)}</button>}</td></tr>))}</tbody></table></div>
        )}
      </Panel>
    </div>
  )
}

function Trace({ id, onBack }: { id: string; onBack: () => void }) {
  const t = useApi<any>(`/audit/trace/${id}`, ['order.update', 'fill.received'], 1500)
  const d = t.data
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3"><button className="btn" onClick={onBack}>← Audit trail</button><h1 className="text-base font-semibold">Why did this happen?</h1><span className="num text-xs text-muted">correlation {id}</span></div>
      <ErrorText error={t.error} />
      {!d ? <Loading /> : <>
        <Step n={1} title="Strategy signal">
          {d.signals.length === 0 ? <Empty>No strategy signal (e.g. kill-switch emergency close or reconciliation)</Empty> : d.signals.map((s: any) => (
            <div key={s.id} className="space-y-1 text-xs"><div className="flex flex-wrap items-center gap-2"><Link className="text-info" to={`/strategies/${s.strategy_id}`}>{s.strategy_id}</Link><StatusBadge status={s.signal} /><span>{s.symbol} @ {fmtNum(s.price)}</span><Badge>{s.origin}</Badge><span className="text-muted">→ {s.action}</span><span className="num text-muted">{fmtTime(s.created_at)}</span></div>
              <div>{s.reason}</div><JsonKV data={s.indicators} /></div>))}
        </Step>
        <Step n={2} title="Risk Firewall">
          {d.risk.length === 0 ? <Empty>No risk evaluation (no order was created)</Empty> : d.risk.map((r: any) => (
            <div key={r.id} className="space-y-1"><div className="flex items-center gap-2 text-xs"><StatusBadge status={r.decision} /><span>{r.message}</span></div><RiskChecks checks={r.checks} /></div>))}
        </Step>
        <Step n={3} title="Order lifecycle">
          {d.orders.length === 0 ? <Empty>No order</Empty> : d.orders.map((o: any) => (
            <div key={o.id} className="space-y-1 text-xs">
              <div className="flex flex-wrap items-center gap-2"><span className="num">{shortId(o.id)}</span><Side side={o.side} /><span className="num">{fmtQty(o.quantity)} {o.symbol} {o.order_type}</span><StatusBadge status={o.status} /><span className="text-muted">key {o.client_order_id}</span></div>
              {o.reject_reason && <div className="text-red-300">{o.reject_reason}</div>}
              <div className="flex flex-wrap items-center gap-1">{o.lifecycle.map((e: any, i: number) => <span key={i} className="flex items-center gap-1" title={e.message}>{i > 0 && <span className="text-muted">→</span>}<StatusBadge status={e.status} /><span className="num text-[10px] text-muted">{fmtTime(e.at)}</span></span>)}</div>
            </div>))}
        </Step>
        <Step n={4} title="Fills (broker executions)">
          {d.fills.length === 0 ? <Empty>No fills</Empty> : <table className="tbl"><thead><tr><th>Time</th><th>Fill</th><th>Side</th><th>Qty</th><th>Price</th><th>Slippage</th><th>Latency</th><th>Source</th></tr></thead>
            <tbody>{d.fills.map((f: any) => <tr key={f.id}><td className="num text-muted">{fmtTime(f.filled_at)}</td><td className="num">{f.broker_fill_id}</td><td><Side side={f.side} /></td><td className="num">{fmtQty(f.quantity)}</td><td className="num">{fmtNum(f.price)}</td><td className="num">{fmtNum(f.slippage_bps, 1)} bps</td><td className="num">{f.latency_ms} ms</td><td>{f.source}</td></tr>)}</tbody></table>}
        </Step>
        <Step n={5} title="Position & P&L impact">
          {d.position_events.length === 0 ? <Empty>No position change</Empty> : <table className="tbl"><thead><tr><th>Time</th><th>Strategy</th><th>Symbol</th><th>Event</th><th>Qty</th><th>Avg</th><th>Realized Δ</th></tr></thead>
            <tbody>{d.position_events.map((p: any, i: number) => <tr key={i}><td className="num text-muted">{fmtTime(p.at)}</td><td>{p.strategy_id}</td><td>{p.symbol}</td><td>{p.event_type}</td><td className="num">{fmtQty(p.qty_before)} → {fmtQty(p.qty_after)}</td><td className="num">{fmtNum(p.avg_after)}</td><td className={`num ${pnlClass(p.realized_delta)}`}>{fmtSigned(p.realized_delta, 2)}</td></tr>)}</tbody></table>}
        </Step>
        <Step n={6} title="Related system events">
          {d.events.length === 0 ? <Empty>None</Empty> : d.events.map((e: any) => <div key={e.id} className="text-xs"><span className="num text-muted">{fmtTime(e.created_at)}</span> <Badge tone={SEV[e.severity] ?? 'gray'}>{e.event_type}</Badge> {e.message}</div>)}
        </Step>
      </>}
    </div>
  )
}

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return <Panel title={<span><span className="mr-2 rounded bg-info/20 px-1.5 text-info">{n}</span>{title}</span>} bodyClass="p-3 space-y-2">{children}</Panel>
}
