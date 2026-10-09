import { ReactNode, useEffect } from 'react'
import { X } from 'lucide-react'
import { fmtINR, fmtNum } from '../services/format'

type Tone = 'green' | 'red' | 'amber' | 'blue' | 'gray' | 'purple'
const TONES: Record<Tone, string> = {
  green: 'bg-green-500/10 text-green-400 border-green-500/30',
  red: 'bg-red-500/10 text-red-400 border-red-500/30',
  amber: 'bg-amber-500/10 text-amber-400 border-amber-500/30',
  blue: 'bg-blue-500/10 text-blue-400 border-blue-500/30',
  gray: 'bg-slate-500/10 text-slate-400 border-slate-500/30',
  purple: 'bg-violet-500/10 text-violet-300 border-violet-500/30',
}

const STATUS_TONE: Record<string, Tone> = {
  HEALTHY: 'green', RUNNING: 'green', LIVE: 'green', FILLED: 'green', APPROVED: 'green', MATCHED: 'green', PASS: 'green',
  SAFE: 'green', COMPLETED: 'green', ok: 'green', done: 'green', BUY: 'green', LONG: 'green', REPLAY: 'blue', CONNECTED: 'green',
  WARNING: 'amber', DELAYED: 'amber', STALE: 'amber', PAUSED: 'amber', PARTIALLY_FILLED: 'amber', WARN: 'amber', TIMEOUT: 'amber',
  MISMATCH_RESOLVED: 'amber', warn: 'amber', ABORTED: 'amber', PAUSING: 'amber', running: 'blue',
  FAILED: 'red', DISCONNECTED: 'red', REJECTED: 'red', HALTED: 'red', SAFE_MODE: 'red', BREACH: 'red', FAIL: 'red',
  MISMATCH_UNRESOLVED: 'red', error: 'red', SELL: 'red', SHORT: 'red', CRITICAL: 'red', ERROR: 'red', failed: 'red', UNAVAILABLE: 'red',
  CREATED: 'gray', RISK_CHECK: 'blue', SUBMITTED: 'blue', ACKNOWLEDGED: 'blue', CANCELLED: 'gray', HOLD: 'gray', FLAT: 'gray',
  SKIP: 'gray', STOPPED: 'gray', IDLE: 'gray', info: 'blue', INFO: 'blue', RUNNING_: 'blue',
}

export function Badge({ children, tone = 'gray', className = '' }: { children: ReactNode; tone?: Tone; className?: string }) {
  return <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-[1px] text-[11px] font-semibold uppercase tracking-wide ${TONES[tone]} ${className}`}>{children}</span>
}

export function StatusBadge({ status, label }: { status?: string | null; label?: ReactNode }) {
  if (!status) return <Badge>—</Badge>
  return <Badge tone={STATUS_TONE[status] ?? 'gray'}>{label ?? status.replace(/_/g, ' ')}</Badge>
}

export function Panel({ title, right, children, className = '', bodyClass = '' }: { title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; bodyClass?: string }) {
  return (
    <section className={`panel min-w-0 ${className}`}>
      {(title || right) && <div className="panel-h"><span>{title}</span><div className="flex items-center gap-2 normal-case tracking-normal">{right}</div></div>}
      <div className={bodyClass}>{children}</div>
    </section>
  )
}

export function Stat({ label, value, sub, className = '' }: { label: string; value: ReactNode; sub?: ReactNode; className?: string }) {
  return (
    <div className="min-w-0 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className={`num text-[15px] font-semibold ${className}`}>{value}</div>
      {sub !== undefined && <div className="text-[11px] text-muted num">{sub}</div>}
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="px-3 py-6 text-center text-muted text-xs">{children}</div>
}

export function ErrorText({ error }: { error?: string | null }) {
  if (!error) return null
  return <div className="rounded border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">{error}</div>
}

export function Loading() { return <div className="p-4 text-xs text-muted">Loading…</div> }

export function Modal({ open, onClose, title, children, footer, wide }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    if (open) window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [open, onClose])
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onMouseDown={onClose}>
      <div className={`panel w-full ${wide ? 'max-w-3xl' : 'max-w-md'} max-h-[90vh] overflow-auto shadow-2xl`} onMouseDown={(e) => e.stopPropagation()}>
        <div className="panel-h"><span>{title}</span><button onClick={onClose} className="text-muted hover:text-text"><X size={14} /></button></div>
        <div className="p-4 space-y-3">{children}</div>
        {footer && <div className="flex justify-end gap-2 border-t border-line px-4 py-3">{footer}</div>}
      </div>
    </div>
  )
}

export function Drawer({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    if (open) window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [open, onClose])
  if (!open) return null
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/50" onMouseDown={onClose}>
      <div className="h-full w-full max-w-2xl overflow-auto border-l border-line bg-panel" onMouseDown={(e) => e.stopPropagation()}>
        <div className="panel-h sticky top-0 bg-panel z-10"><span>{title}</span><button onClick={onClose} className="text-muted hover:text-text"><X size={16} /></button></div>
        <div className="p-4 space-y-4">{children}</div>
      </div>
    </div>
  )
}

export interface MeterData { label: string; used: number; limit: number; unit: string; pct: number; level: string }
export function Meter({ m }: { m: MeterData }) {
  const color = m.level === 'BREACH' ? 'bg-red-500' : m.level === 'WARNING' ? 'bg-amber-500' : 'bg-green-500'
  const fmt = (v: number) => (m.unit === 'INR' ? fmtINR(v) : `${fmtNum(v, 0)}${m.unit === 'shares' ? '' : ''}`)
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2 text-xs">
        <span className="text-muted">{m.label}</span>
        <span className="flex items-center gap-2"><span className="num">{fmt(m.used)} / {fmt(m.limit)}{m.unit === 'shares' ? ' sh' : ''}</span><StatusBadge status={m.level} /></span>
      </div>
      <div className="h-1.5 w-full rounded bg-line"><div className={`h-1.5 rounded ${color}`} style={{ width: `${Math.min(100, Math.max(0, m.pct))}%` }} /></div>
    </div>
  )
}

export function Side({ side }: { side?: string | null }) {
  if (!side) return <span>—</span>
  return <span className={`font-semibold ${side === 'BUY' || side === 'LONG' ? 'text-pos' : side === 'SELL' || side === 'SHORT' ? 'text-neg' : 'text-muted'}`}>{side}</span>
}

export function JsonKV({ data }: { data?: Record<string, any> | null }) {
  if (!data || !Object.keys(data).length) return <span className="text-muted">—</span>
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px]">
      {Object.entries(data).map(([k, v]) => <span key={k}><span className="text-muted">{k}</span> <span className="num">{typeof v === 'number' ? fmtNum(v, 4) : String(v)}</span></span>)}
    </div>
  )
}
