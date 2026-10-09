import { useEffect, useState } from 'react'
import { api } from '../services/api'
import { useEvents } from '../hooks/useEvents'
import { WsEvent } from '../services/ws'
import { fmtNum, fmtQty, fmtTime } from '../services/format'
import { Empty } from './ui'

const TYPES = ['strategy.signal', 'risk.decision', 'order.update', 'fill.received', 'reconciliation.completed', 'fault.step',
  'kill_switch.update', 'trading.status', 'strategy.status', 'system.alert', 'broker.status', 'demo.update']

interface Row { id: string; at: string; tone: string; tag: string; text: string }

function describe(e: WsEvent): Row | null {
  const p = e.payload || {}
  const base = { id: e.event_id || Math.random().toString(36), at: e.timestamp || new Date().toISOString() }
  switch (e.event_type) {
    case 'strategy.signal': return { ...base, tone: p.signal === 'BUY' ? 'text-pos' : 'text-neg', tag: 'SIGNAL', text: `${e.strategy_id} ${p.signal} ${e.symbol} @ ${fmtNum(p.price)} — ${p.reason} → ${p.action}` }
    case 'risk.decision': return { ...base, tone: p.approved ? 'text-pos' : 'text-neg', tag: p.approved ? 'RISK OK' : 'RISK REJECT', text: p.message }
    case 'order.update': {
      const o = p.order || {}
      if (!['SUBMITTED', 'ACKNOWLEDGED', 'FILLED', 'REJECTED', 'CANCELLED', 'TIMEOUT', 'PARTIALLY_FILLED'].includes(p.transition)) return null
      return { ...base, tone: p.transition === 'REJECTED' ? 'text-neg' : p.transition === 'FILLED' ? 'text-pos' : ['TIMEOUT', 'PARTIALLY_FILLED'].includes(p.transition) ? 'text-warn' : 'text-blue-300', tag: 'ORDER', text: `${o.id?.slice(0, 8)} ${o.strategy_id} ${o.side} ${fmtQty(o.quantity)} ${o.symbol} → ${p.transition}: ${p.message}` }
    }
    case 'fill.received': return { ...base, tone: 'text-pos', tag: 'FILL', text: `${e.strategy_id} ${p.side} ${fmtQty(p.quantity)} ${e.symbol} @ ${fmtNum(p.price)} (slip ${fmtNum(p.slippage_bps, 1)} bps, ${p.latency_ms} ms)` }
    case 'reconciliation.completed': return { ...base, tone: p.status === 'MATCHED' ? 'text-pos' : p.status === 'MISMATCH_RESOLVED' ? 'text-warn' : 'text-neg', tag: 'RECON', text: `${p.trigger}: ${p.status} — ${p.summary}` }
    case 'fault.step': return { ...base, tone: p.step?.status === 'error' ? 'text-neg' : p.step?.status === 'ok' ? 'text-pos' : 'text-warn', tag: 'FAULT', text: `${p.scenario}: ${p.step?.title} ${p.step?.detail ? '— ' + p.step.detail : ''}` }
    case 'kill_switch.update': return { ...base, tone: 'text-neg', tag: 'KILL', text: `kill switch ${p.phase ?? ''} ${p.active ? '(active)' : ''}` }
    case 'trading.status': return { ...base, tone: p.status === 'RUNNING' ? 'text-pos' : p.status === 'SAFE_MODE' ? 'text-neg' : 'text-warn', tag: 'TRADING', text: `${p.status}: ${p.reason}` }
    case 'strategy.status': return { ...base, tone: p.status === 'RUNNING' ? 'text-pos' : 'text-warn', tag: 'STRATEGY', text: `${e.strategy_id} → ${p.status}${p.reason ? ' (' + p.reason + ')' : ''}` }
    case 'system.alert': return { ...base, tone: 'text-neg', tag: 'ALERT', text: `${p.component}: ${p.detail}` }
    case 'broker.status': return { ...base, tone: p.available ? 'text-pos' : 'text-neg', tag: 'BROKER', text: p.available ? 'broker available' : `broker unavailable: ${p.last_error}` }
    default: return null
  }
}

export default function EventFeed({ max = 80, height = 'h-80' }: { max?: number; height?: string }) {
  const [rows, setRows] = useState<Row[]>([])
  useEvents(TYPES, (e) => { const r = describe(e); if (r) setRows((x) => (x.some((y) => y.id === r.id) ? x : [r, ...x].slice(0, max))) })
  // seed with recent history from the persisted audit trail so the feed is meaningful on arrival
  useEffect(() => {
    api.get<any[]>(`/audit?limit=${Math.min(max, 40)}`).then((ev) => {
      const hist: Row[] = ev.map((a) => ({
        id: `audit-${a.id}`, at: a.created_at, tag: a.event_type.replace(/_/g, ' ').slice(0, 14),
        tone: a.severity === 'CRITICAL' || a.severity === 'ERROR' ? 'text-neg' : a.severity === 'WARN' ? 'text-warn' : 'text-muted',
        text: a.message,
      }))
      setRows((x) => {
        const seen = new Set(x.map((r) => r.id))
        return [...x, ...hist.filter((r) => !seen.has(r.id))].slice(0, max)
      })
    }).catch(() => { /* live events still arrive */ })
  }, [max])
  return (
    <div className={`${height} overflow-auto font-mono text-[11px]`}>
      {rows.length === 0 ? <Empty>Waiting for live events…</Empty> : rows.map((r) => (
        <div key={r.id} className="flex gap-2 border-b border-[#161e29] px-3 py-1">
          <span className="shrink-0 text-muted">{fmtTime(r.at)}</span>
          <span className={`w-20 shrink-0 font-semibold ${r.tone}`}>{r.tag}</span>
          <span className="min-w-0 break-words text-text">{r.text}</span>
        </div>
      ))}
    </div>
  )
}
