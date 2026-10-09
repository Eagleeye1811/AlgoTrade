import { fmtTime } from '../services/format'
import { StatusBadge } from './ui'

const PHASES = [
  ['FAULT_INJECTED', 'Fault injected'], ['SYSTEM_RESPONSE', 'System response'], ['RECOVERY_ACTION', 'Recovery action'], ['FINAL_STATE', 'Final state'],
] as const
const DOT: Record<string, string> = { ok: 'border-green-500/50 bg-green-500/5', warn: 'border-amber-500/50 bg-amber-500/5', error: 'border-red-500/50 bg-red-500/5', info: 'border-blue-500/40 bg-blue-500/5' }

export default function FaultTimeline({ steps }: { steps: any[] }) {
  return (
    <div className="grid gap-2 md:grid-cols-4">
      {PHASES.map(([key, label], i) => {
        const items = steps.filter((s) => s.phase === key)
        return (
          <div key={key} className="min-w-0">
            <div className="mb-1 flex items-center gap-1 text-[11px] font-semibold uppercase tracking-wider text-muted">{i + 1}. {label}{i < 3 && <span className="ml-auto hidden md:inline">→</span>}</div>
            <div className="space-y-1.5">
              {items.length === 0 ? <div className="rounded border border-dashed border-line p-2 text-[11px] text-muted">—</div> : items.map((s, j) => (
                <div key={j} className={`rounded border p-2 text-xs ${DOT[s.status] ?? DOT.info}`}>
                  <div className="flex items-start justify-between gap-2"><span className="font-semibold">{s.title}</span><StatusBadge status={s.status} /></div>
                  {s.detail && <div className="mt-0.5 break-words text-muted">{s.detail}</div>}
                  <div className="num mt-0.5 text-[10px] text-muted">{fmtTime(s.at)}</div>
                </div>
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}
