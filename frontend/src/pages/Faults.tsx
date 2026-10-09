import { Fragment, useEffect, useState } from 'react'
import { useAuth } from '../hooks/auth'
import { useSystem } from '../hooks/system'
import { useApi, useEvents } from '../hooks/useEvents'
import { api } from '../services/api'
import { fmtTime } from '../services/format'
import FaultTimeline from '../components/FaultTimeline'
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
  const [strategy, setStrategy] = useState('macd')
  const [err, setErr] = useState<string | null>(null)
  const [resetOpen, setResetOpen] = useState(false)
  const [openRun, setOpenRun] = useState<string | null>(null)
  useEvents(['fault.step'], (e) => setLive((r) => {
    const p = e.payload
    if (!r || r.run_id !== p.run_id) return { run_id: p.run_id, scenario: p.scenario, steps: [p.step], status: 'RUNNING' }
    return { ...r, steps: [...r.steps, p.step] }
  }))
  useEvents(['fault.completed'], (e) => setLive((r) => (r && r.run_id === e.payload.run_id ? { ...r, status: e.payload.status, outcome: e.payload.outcome, steps: e.payload.steps } : r)))
  useEvents(['chaos.started', 'chaos.progress', 'chaos.completed'], (e) => setChaos({ ...e.payload, phase: e.event_type }))
  useEffect(() => { if (!live && runs.data?.length) { const r = runs.data[0]; setLive({ run_id: r.id, scenario: r.scenario, steps: r.steps, status: r.status, outcome: r.outcome }) } }, [runs.data, live])

  const call = async (path: string, body?: any) => { setErr(null); try { await api.post(path, body); sc.reload(); demo.reload() } catch (e: any) { setErr(e.message) } }
  const busy = !!sc.data?.active || sc.data?.chaos_running
  const titles: Record<string, string> = Object.fromEntries((sc.data?.scenarios || []).map((s: any) => [s.key, s.title]))
  const trading = overview?.trading
  return (
    <div className="space-y-3">
      <h1 className="text-base font-semibold">Fault Simulation &amp; Recovery Lab</h1>
      <div className="text-xs text-muted">Every scenario injects a real fault into a real component (paper broker transport, market feed, strategy logic, internal state) and the timeline is built from what the engine actually did.
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

      <Panel title="Inject a fault" right={<>
        <span className="text-[11px] text-muted">Target strategy</span>
        <select className="input" value={strategy} onChange={(e) => setStrategy(e.target.value)}><option value="rsi_ema">RSI + EMA</option><option value="macd">MACD</option><option value="breakout">Breakout</option></select>
        <button className="btn btn-primary" disabled={busy} onClick={() => call('/faults/chaos')}>Run automated chaos mode</button></>} bodyClass="p-3">
        {!sc.data ? <Loading /> : (
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            {sc.data.scenarios.map((s: any) => (
              <div key={s.key} className="flex flex-col justify-between gap-2 rounded border border-line bg-panel2 p-2.5 text-xs">
                <div><div className="font-semibold text-white">{s.title}</div><div className="text-muted">{s.description}</div></div>
                <button className="btn self-start" disabled={busy} onClick={() => call(`/faults/run/${s.key}`, { strategy_id: strategy })}>{sc.data.active?.scenario === s.key ? 'Running…' : 'Inject'}</button>
              </div>
            ))}
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

      <Panel title={live ? `Live timeline · ${titles[live.scenario] || live.scenario}` : 'Live timeline'} right={live && <StatusBadge status={live.status} />} bodyClass="p-3 space-y-2">
        {!live ? <Empty>Inject a fault to see FAULT INJECTED → SYSTEM RESPONSE → RECOVERY ACTION → FINAL STATE</Empty> : <>
          <FaultTimeline steps={live.steps} />
          {live.outcome && <div className="text-xs"><span className="text-muted">Outcome:</span> {live.outcome}</div>}
        </>}
      </Panel>

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
        <p className="text-sm">This permanently deletes all orders, fills, positions, signals, risk/reconciliation events, fault runs and the audit trail, resets the paper broker account to its starting capital and rewinds the market replay.</p>
        <p className="text-xs text-muted">Users, strategies, configurations and risk limits are kept. Trading will be PAUSED afterwards.</p>
      </Modal>
    </div>
  )
}
