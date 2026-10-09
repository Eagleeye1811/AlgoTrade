import { Fragment, useEffect, useState } from 'react'
import { useAuth } from '../hooks/auth'
import { useSystem } from '../hooks/system'
import { useApi, useEvents } from '../hooks/useEvents'
import { api } from '../services/api'
import { fmtTime } from '../services/format'
import FaultTimeline from '../components/FaultTimeline'
import IncidentConsole from '../components/IncidentConsole'
import { Bot } from 'lucide-react'
import { Badge, Empty, ErrorText, Loading, Modal, Panel, StatusBadge } from '../components/ui'

interface LiveRun { run_id: string; scenario: string; steps: any[]; status: string; outcome?: string }

export default function Faults() {
  const { user } = useAuth()
  const { overview } = useSystem()
  const sc = useApi<any>('/faults/scenarios', ['fault.completed', 'chaos.completed', 'chaos.started'], 500)
  const runs = useApi<any[]>('/faults/runs?limit=30', ['fault.completed'], 500)
  const demo = useApi<any>('/demo')
  useEvents(['demo.update'], (e) => demo.setData(e.payload))
  const [live, setLive] = useState<LiveRun | null>(null)
  const [chaos, setChaos] = useState<any>(null)
  const strategies = useApi<any[]>('/strategies', ['strategy.status'], 2000)
  // each scenario has its own target strategy (defaults to the first running one)
  const [targets, setTargets] = useState<Record<string, string>>({})
  const firstRunning = strategies.data?.find((x: any) => x.status === 'RUNNING')?.id || strategies.data?.[0]?.id || ''
  const targetFor = (key: string) => targets[key] || firstRunning
  const [err, setErr] = useState<string | null>(null)
  const [resetOpen, setResetOpen] = useState(false)
  const [openRun, setOpenRun] = useState<string | null>(null)
  useEvents(['fault.step'], (e) => setLive((r) => {
    const p = e.payload
    if (!r || (r.run_id && r.run_id !== p.run_id)) return { run_id: p.run_id, scenario: p.scenario, steps: [p.step], status: 'RUNNING' }
    if (!r.run_id) return { ...r, run_id: p.run_id, steps: [...r.steps, p.step] }
    return { ...r, steps: [...r.steps, p.step] }
  }))
  useEvents(['fault.completed'], (e) => setLive((r) => (r && r.run_id === e.payload.run_id ? { ...r, status: e.payload.status, outcome: e.payload.outcome, steps: e.payload.steps } : r)))
  useEvents(['chaos.started', 'chaos.progress', 'chaos.completed'], (e) => setChaos({ ...e.payload, phase: e.event_type }))
  useEffect(() => { if (!live && runs.data?.length) { const r = runs.data[0]; setLive({ run_id: r.id, scenario: r.scenario, steps: r.steps, status: r.status, outcome: r.outcome }) } }, [runs.data, live])
  // latest run per scenario, for the result shown on each card
  const lastRun: Record<string, any> = {}
  for (const r of runs.data || []) if (!lastRun[r.scenario]) lastRun[r.scenario] = r
  const show = (r: any) => {
    setLive({ run_id: r.id, scenario: r.scenario, steps: r.steps, status: r.status, outcome: r.outcome })
    document.getElementById('fault-timeline')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
  const inject = async (key: string) => {
    setLive({ run_id: '', scenario: key, steps: [], status: 'RUNNING' })
    document.getElementById('fault-timeline')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    await call(`/faults/run/${key}`, { strategy_id: targetFor(key) })
    runs.reload()
  }

  const call = async (path: string, body?: any) => { setErr(null); try { await api.post(path, body); sc.reload(); demo.reload() } catch (e: any) { setErr(e.message) } }
  const busy = !!sc.data?.active || sc.data?.chaos_running
  const titles: Record<string, string> = Object.fromEntries((sc.data?.scenarios || []).map((s: any) => [s.key, s.title]))
  const trading = overview?.trading
  return (
    <div className="space-y-3">
      <h1 className="text-base font-semibold">Fault Simulation &amp; Recovery Lab</h1>
      <div className="text-xs text-muted">Every scenario injects a real fault into a real component (021 broker adapter boundary, market feed, strategy logic, internal state) and the timeline is built from what the engine actually did.
        Test orders go through the full Signal → Risk → Order → Broker path, so trading must be <b>RUNNING</b> {trading && <>— currently <StatusBadge status={trading.status} /></>}.</div>
      <ErrorText error={err} />

      <Panel title="Demo mode — scripted judge walkthrough on the real engine" right={<StatusBadge status={demo.data?.status} />} bodyClass="p-3 space-y-3">
        <div className="flex flex-wrap gap-2">
          <button className="btn btn-green" disabled={demo.data?.status === 'RUNNING' || demo.data?.status === 'PAUSED' || demo.data?.status === 'PAUSING'} onClick={() => call('/demo/start')}>Start demo</button>
          {demo.data?.status === 'RUNNING' && <button className="btn" onClick={() => call('/demo/pause')}>Pause demo</button>}
          {(demo.data?.status === 'PAUSED' || demo.data?.status === 'PAUSING') && <button className="btn" onClick={() => call('/demo/resume')}>Resume demo</button>}
          {user?.role === 'ADMIN' && <button className="btn btn-danger" onClick={() => setResetOpen(true)}>Reset demo</button>}
        </div>
        {demo.data && (
          <ol className="grid gap-1.5 text-xs sm:grid-cols-2 xl:grid-cols-3">
            {demo.data.plan.map((p: any, i: number) => {
              const st = demo.data.steps.find((s: any) => s.key === p.key)
              return (
                <li key={p.key} className={`rounded border p-2 ${st?.status === 'done' ? 'border-green-500/40' : st?.status === 'failed' ? 'border-red-500/40' : st?.status === 'running' ? 'border-blue-500/50 animate-pulse' : 'border-line'}`}>
                  <div className="flex items-center justify-between gap-2"><span className="font-semibold">{i + 1}. {p.title}</span>{st ? <StatusBadge status={st.status} /> : <Badge>pending</Badge>}</div>
                  <div className="text-muted">{st?.detail || p.description}</div>
                </li>
              )
            })}
          </ol>
        )}
      </Panel>

      <Panel title="Inject a fault" right={<div className="flex items-center gap-2">
          <button title="After every fault: reconcile with 021, check broker / market data / blocks / SAFE MODE, and resume trading only if everything passes"
            disabled={user?.role !== 'ADMIN' || !sc.data}
            onClick={async () => { setErr(null); try { await api.put('/faults/autopilot', { enabled: !sc.data?.autopilot }); sc.reload() } catch (e: any) { setErr(e.message) } }}
            className={`btn flex items-center gap-1.5 ${sc.data?.autopilot ? 'border-green-500/60 bg-green-500/15 text-green-300' : ''}`}>
            <Bot size={14} /> Self-healing autopilot: <b>{sc.data?.autopilot ? 'ON' : 'OFF'}</b></button>
          <button className="btn btn-primary" disabled={busy} onClick={() => call('/faults/chaos')}>Run automated chaos mode</button></div>} bodyClass="p-3">
        {!sc.data ? <Loading /> : (
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
            {sc.data.scenarios.map((s: any) => {
              const r = lastRun[s.key]
              const running = sc.data.active?.scenario === s.key
              return (
                <div key={s.key} className={`flex flex-col gap-2 rounded border bg-panel2 p-3 text-xs ${running ? 'border-blue-500/60' : 'border-line'}`}>
                  <div>
                    <div className="flex items-center justify-between gap-2"><span className="text-sm font-semibold text-white">{s.title}</span>
                      {running ? <StatusBadge status="running" label="RUNNING" /> : r && <StatusBadge status={r.status} />}</div>
                    <div className="mt-0.5 text-muted">{s.description}</div>
                  </div>
                  <label className="text-[11px] text-muted">Target strategy
                    <select className="input mt-0.5 w-full" value={targetFor(s.key)} disabled={s.available === false}
                      onChange={(e) => setTargets({ ...targets, [s.key]: e.target.value })}>
                      {(strategies.data || []).map((x: any) => <option key={x.id} value={x.id}>{x.name} · {x.symbol} · {x.status}</option>)}
                    </select></label>
                  <div className="flex flex-wrap items-center gap-2">
                    <button className="btn btn-primary" disabled={busy || s.available === false || !targetFor(s.key)}
                      title={s.available === false ? 'Needs the local simulator: a real exchange cannot be forced to do this' : undefined}
                      onClick={() => inject(s.key)}>{s.available === false ? 'Simulator only' : running ? 'Running…' : 'Inject'}</button>
                    {r && <button className="btn" onClick={() => show(r)}>View timeline</button>}
                  </div>
                  {r && (
                    <div className="rounded border border-line bg-bg p-2">
                      <div className="flex items-center justify-between text-[10px] text-muted"><span>Last run · {fmtTime(r.started_at, true)}</span><span>{r.triggered_by}</span></div>
                      <div className="mt-0.5 whitespace-pre-wrap break-words">{r.outcome || '—'}</div>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
        {chaos && (
          <div className="mt-3 rounded border border-line p-2 text-xs">
            <div className="mb-1 font-semibold">Chaos mode {chaos.phase === 'chaos.completed' ? `finished — final reconciliation ${chaos.final_reconciliation}, orders allowed: ${String(chaos.orders_allowed)}` : 'running'}</div>
            <div className="flex flex-wrap gap-2">{(sc.data?.chaos_sequence || chaos.sequence || []).map((k: string) => {
              const r = (chaos.results || []).find((x: any) => x.scenario === k)
              return <span key={k} className="flex items-center gap-1">{titles[k] || k} {r ? <StatusBadge status={r.status} /> : <Badge>pending</Badge>}</span>
            })}</div>
          </div>
        )}
      </Panel>

      <div id="fault-timeline" className="scroll-mt-4">
        <Panel title={live ? `Timeline · ${titles[live.scenario] || live.scenario}` : 'Timeline'} right={live && <StatusBadge status={live.status} />} bodyClass="p-3 space-y-3">
          {!live ? <Empty>Inject a fault to see FAULT INJECTED → SYSTEM RESPONSE → RECOVERY ACTION → FINAL STATE</Empty> : <>
            <IncidentConsole run={live} title={titles[live.scenario] || live.scenario} />
            {live.outcome && (
              <div className={`rounded border p-2.5 text-xs ${live.status === 'COMPLETED' ? 'border-green-500/40 bg-green-500/5' : 'border-amber-500/40 bg-amber-500/5'}`}>
                <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted">Outcome</div>
                <div className="whitespace-pre-wrap break-words">{live.outcome}</div>
              </div>
            )}
          </>}
        </Panel>
      </div>

      <Panel title="Run history">
        {!runs.data ? <Loading /> : runs.data.length === 0 ? <Empty>No fault runs yet</Empty> : (
          <div className="overflow-x-auto"><table className="tbl"><thead><tr><th>Started</th><th>Scenario</th><th>Status</th><th>By</th><th>Group</th><th>Outcome</th></tr></thead>
            <tbody>{runs.data.map((r) => (
              <Fragment key={r.id}>
                <tr className="clickable" onClick={() => setOpenRun(openRun === r.id ? null : r.id)}>
                  <td className="num text-muted">{fmtTime(r.started_at, true)}</td><td>{titles[r.scenario] || r.scenario}</td><td><StatusBadge status={r.status} /></td>
                  <td className="text-muted">{r.triggered_by}</td><td className="num text-muted">{r.run_group ? r.run_group.slice(0, 6) : '—'}</td><td className="max-w-[420px] truncate">{r.outcome}</td>
                </tr>
                {openRun === r.id && <tr><td colSpan={6} className="bg-bg p-3 whitespace-normal"><FaultTimeline steps={r.steps} /></td></tr>}
              </Fragment>))}</tbody></table></div>
        )}
      </Panel>

      <Modal open={resetOpen} onClose={() => setResetOpen(false)} title="Reset demo"
        footer={<><button className="btn" onClick={() => setResetOpen(false)}>Cancel</button><button className="btn btn-danger" onClick={async () => { setResetOpen(false); await call('/demo/reset') }}>Wipe & reset</button></>}>
        <p className="text-sm">This permanently deletes all orders, fills, positions, signals, risk/reconciliation events, fault runs and the audit trail, and the platform's own trading history. The 021 account must be flat first (its orders stay in 021's order book).</p>
        <p className="text-xs text-muted">Users, strategies, configurations and risk limits are kept. Trading will be PAUSED afterwards.</p>
      </Modal>
    </div>
  )
}
