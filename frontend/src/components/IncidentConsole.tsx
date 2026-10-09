import { useEffect, useRef, useState } from 'react'
import { Activity, Bot, CandlestickChart, Database, GitCompare, Landmark, ListOrdered, ShieldCheck, Timer, Wallet } from 'lucide-react'

/** Live incident console for a fault run: stopwatch, phase tracker, system map, streaming event feed, scorecard. */

const PHASES = [
  ['FAULT_INJECTED', 'Inject'], ['SYSTEM_RESPONSE', 'Respond'], ['RECOVERY_ACTION', 'Recover'], ['FINAL_STATE', 'Final'], ['AUTOPILOT', 'Autopilot'],
] as const
const PHASE_LABEL: Record<string, string> = Object.fromEntries(PHASES)

// the platform's components, in signal -> order -> broker order, plus the state keepers
const NODES = [
  { key: 'market', label: 'Market data', icon: CandlestickChart, re: /market|feed|price|stale|tick|candle|data/i },
  { key: 'strategy', label: 'Strategy', icon: Activity, re: /strateg|signal|runaway|malfunction|halt/i },
  { key: 'risk', label: 'Risk Firewall', icon: ShieldCheck, re: /risk|firewall|rejected|circuit|rule|oversized|limit|blocked/i },
  { key: 'orders', label: 'Order Engine', icon: ListOrdered, re: /order|duplicate|idempoten|submission|uncertain/i },
  { key: 'broker', label: '021 Broker', icon: Landmark, re: /broker|021|api|latency|timeout|outage|connection|reachable/i },
  { key: 'positions', label: 'Positions', icon: Wallet, re: /position|flatten|cache|fill/i },
  { key: 'recon', label: 'Reconciliation', icon: GitCompare, re: /reconcil|matches|mismatch/i },
  { key: 'engine', label: 'Trading engine', icon: Database, re: /trading|engine|restart|postgres|state|safe mode|resum|autopilot|checks/i },
] as const
const PIPE = ['market', 'strategy', 'risk', 'orders', 'broker']
const RANK: Record<string, number> = { info: 0, ok: 1, warn: 2, error: 3 }
const TONE: Record<string, string> = {
  ok: 'border-green-500/60 bg-green-500/10 text-green-300', warn: 'border-amber-500/60 bg-amber-500/10 text-amber-300',
  error: 'border-red-500/60 bg-red-500/10 text-red-300', info: 'border-sky-500/50 bg-sky-500/10 text-sky-300', idle: 'border-line bg-bg text-muted',
}
const ms = (a?: string, b?: string) => (a && b ? Date.parse(b) - Date.parse(a) : null)
const fmtDur = (v: number | null) => (v == null ? '—' : v < 1000 ? `${Math.max(0, Math.round(v))} ms` : `${(v / 1000).toFixed(v < 10000 ? 2 : 1)} s`)

export interface Run { run_id: string; scenario: string; steps: any[]; status: string; outcome?: string }

export default function IncidentConsole({ run, title }: { run: Run; title: string }) {
  const running = run.status === 'RUNNING'
  const steps = run.steps
  const t0 = steps[0]?.at
  const [now, setNow] = useState(Date.now())
  const started = useRef(Date.now())
  useEffect(() => { started.current = Date.now() }, [run.run_id])
  useEffect(() => {
    if (!running) return
    const id = window.setInterval(() => setNow(Date.now()), 100)
    return () => clearInterval(id)
  }, [running])
  const feed = useRef<HTMLDivElement>(null)
  useEffect(() => { feed.current?.scrollTo({ top: feed.current.scrollHeight, behavior: 'smooth' }) }, [steps.length])

  const elapsed = running ? now - (t0 ? Date.parse(t0) : started.current) : ms(t0, steps[steps.length - 1]?.at)
  const latest = steps[steps.length - 1]

  // per-phase status (worst status seen) and the phase in progress
  const phaseState = (key: string) => {
    const xs = steps.filter((s) => s.phase === key)
    if (!xs.length) return null
    return xs.reduce((w, s) => (RANK[s.status] > RANK[w] ? s.status : w), 'info' as string)
  }
  const usedPhases = PHASES.filter(([k]) => k !== 'AUTOPILOT' || steps.some((s) => s.phase === 'AUTOPILOT'))

  // component health from the steps that mention it; the most recently touched one pulses
  const nodeOf = (s: any) => NODES.filter((n) => n.re.test(`${s.title} ${s.detail ?? ''}`)).map((n) => n.key)
  const nodeState: Record<string, { status: string; title: string }> = {}
  for (const s of steps) for (const k of nodeOf(s)) nodeState[k] = { status: s.status, title: s.title }
  const hot = latest ? nodeOf(latest) : []

  // scorecard
  const firstOf = (p: string) => steps.find((s) => s.phase === p)?.at
  const lastOf = (p: string) => [...steps].reverse().find((s) => s.phase === p)?.at
  const detect = ms(t0, firstOf('SYSTEM_RESPONSE') ?? firstOf('RECOVERY_ACTION'))
  const recover = ms(t0, lastOf('RECOVERY_ACTION') ?? lastOf('FINAL_STATE'))
  const final = [...steps].reverse().find((s) => s.phase === 'FINAL_STATE')
  const pilot = [...steps].reverse().find((s) => s.phase === 'AUTOPILOT' && !/^✔|^✘|engaged|Reconciliation/.test(s.title))
  const errors = steps.filter((s) => s.status === 'error').length

  return (
    <div className="space-y-3">
      {/* stopwatch + phase tracker */}
      <div className="flex flex-wrap items-center gap-4">
        <div className={`flex items-center gap-2 rounded-lg border px-3 py-2 ${running ? 'border-red-500/50 bg-red-500/10' : 'border-line bg-bg'}`}>
          <Timer size={16} className={running ? 'animate-pulse text-red-400' : 'text-muted'} />
          <div>
            <div className="text-[10px] uppercase tracking-wider text-muted">{running ? 'Incident live' : 'Incident duration'}</div>
            <div className="num text-xl font-semibold leading-none text-white">{fmtDur(elapsed)}</div>
          </div>
        </div>
        <div className="flex min-w-[280px] flex-1 items-center">
          {usedPhases.map(([k, label], i) => {
            const st = phaseState(k)
            const active = running && latest?.phase === k
            return (
              <div key={k} className="flex flex-1 items-center">
                <div className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-semibold ${st ? TONE[st] : TONE.idle} ${active ? 'ic-glow' : ''}`}>
                  {k === 'AUTOPILOT' && <Bot size={12} />}{label}
                </div>
                {i < usedPhases.length - 1 && <div className={`mx-1 h-0.5 flex-1 rounded ${phaseState(usedPhases[i + 1][0]) ? 'bg-green-500/50' : running && st ? 'ic-flow' : 'bg-line'}`} />}
              </div>
            )
          })}
        </div>
      </div>

      {/* live system map */}
      <div className="rounded-lg border border-line bg-bg/60 p-3">
        <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-muted">Live system map · signal → risk → order → 021, and the state keepers</div>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
          {NODES.map((n) => {
            const st = nodeState[n.key]
            const Icon = n.icon
            return (
              <div key={n.key} title={st?.title}
                className={`relative rounded-md border p-2 transition-colors duration-500 ${st ? TONE[st.status] : TONE.idle} ${hot.includes(n.key as any) && running ? 'ic-glow' : ''}`}>
                <div className="flex items-center gap-1.5 text-[11px] font-semibold"><Icon size={13} />{n.label}
                  {PIPE.includes(n.key) && PIPE.indexOf(n.key) < PIPE.length - 1 && <span className="ml-auto hidden text-muted lg:inline">→</span>}</div>
                <div className="mt-1 truncate text-[10px] opacity-80">{st ? st.title : 'untouched'}</div>
              </div>
            )
          })}
        </div>
      </div>

      {/* streaming event feed */}
      <div ref={feed} className="max-h-[420px] space-y-1.5 overflow-y-auto pr-1">
        {steps.length === 0 && <div className="flex items-center gap-2 text-xs text-muted"><span className="h-2 w-2 animate-ping rounded-full bg-red-400" /> Injecting fault…</div>}
        {steps.map((s, i) => (
          <div key={i} className={`ic-in flex gap-3 rounded border p-2 text-xs ${TONE[s.status] ?? TONE.info}`}>
            <div className="w-16 shrink-0 text-right">
              <div className="num font-semibold">+{fmtDur(ms(t0, s.at))}</div>
              <div className="text-[10px] uppercase tracking-wider opacity-70">{PHASE_LABEL[s.phase] ?? s.phase}</div>
            </div>
            <div className="min-w-0 flex-1 border-l border-current/20 pl-3">
              <div className="font-semibold text-text">{s.title}</div>
              {s.detail && <div className="mt-0.5 break-words text-muted">{s.detail}</div>}
            </div>
          </div>
        ))}
        {running && steps.length > 0 && <div className="flex items-center gap-2 pl-20 text-[11px] text-muted"><span className="h-1.5 w-1.5 animate-ping rounded-full bg-sky-400" /> watching the engine…</div>}
      </div>

      {/* incident scorecard */}
      {!running && steps.length > 0 && (
        <div className="ic-in grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
          <Score label="Time to detect" value={fmtDur(detect)} tone="sky" />
          <Score label="Time to recover" value={fmtDur(recover)} tone="green" />
          <Score label="Total incident" value={fmtDur(elapsed)} />
          <Score label="Events / errors" value={`${steps.length} / ${errors}`} tone={errors ? 'red' : undefined} />
          <Score label="Final state" value={final ? (final.status === 'ok' ? 'CONSISTENT' : final.status.toUpperCase()) : run.status} tone={final?.status === 'ok' ? 'green' : 'amber'} />
          <Score label="Autopilot" value={pilot ? pilot.title.replace(/^Autopilot /, '') : 'off'} tone={pilot ? (pilot.status === 'ok' ? 'green' : 'amber') : undefined} small />
        </div>
      )}
      <div className="sr-only">{title}</div>
    </div>
  )
}

function Score({ label, value, tone, small }: { label: string; value: string; tone?: 'green' | 'amber' | 'red' | 'sky'; small?: boolean }) {
  const c = tone === 'green' ? 'text-green-300' : tone === 'amber' ? 'text-amber-300' : tone === 'red' ? 'text-red-300' : tone === 'sky' ? 'text-sky-300' : 'text-white'
  return (
    <div className="rounded-md border border-line bg-bg p-2">
      <div className="text-[10px] uppercase tracking-wider text-muted">{label}</div>
      <div className={`num mt-0.5 font-semibold ${small ? 'text-xs' : 'text-lg'} ${c}`}>{value}</div>
    </div>
  )
}
