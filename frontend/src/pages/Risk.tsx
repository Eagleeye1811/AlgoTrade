import { Fragment, useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../hooks/auth'
import { useApi } from '../hooks/useEvents'
import { api } from '../services/api'
import { fmtNum, fmtTime } from '../services/format'
import RiskChecks from '../components/RiskChecks'
import RiskAdvisor from '../components/RiskAdvisor'
import { Empty, ErrorText, Loading, Meter, Panel, StatusBadge } from '../components/ui'

export default function Risk() {
  const { user } = useAuth()
  const risk = useApi<any>('/risk', ['risk.decision', 'pnl.update', 'strategy.status'], 2000)
  const [decision, setDecision] = useState('')
  const events = useApi<any[]>(`/risk/events?limit=100${decision ? `&decision=${decision}` : ''}`, ['risk.decision'], 800)
  const [open, setOpen] = useState<number | null>(null)
  const d = risk.data
  if (!d) return risk.error ? <ErrorText error={risk.error} /> : <Loading />
  const g = d.guard
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3"><h1 className="text-base font-semibold">Risk Firewall &amp; Virtual Margin</h1>
        <span className="text-xs text-muted num">{d.stats.approved} approved · {d.stats.rejected} rejected (since start)</span></div>
      <RiskAdvisor currentLimits={d.limits} isAdmin={user?.role === 'ADMIN'} onLimitsApplied={risk.reload} />
      <div className="grid gap-3 lg:grid-cols-2">
        <Panel title="Portfolio utilization" bodyClass="p-3 space-y-3">{d.portfolio.map((m: any) => <Meter key={m.label} m={m} />)}</Panel>
        <Panel title="Slippage / latency guard (rule-based)" bodyClass="p-3 text-xs space-y-2">
          <Row label="Broker API latency" value={g.broker_latency_ms != null ? `${fmtNum(g.broker_latency_ms, 0)} ms` : 'n/a'}
            note={`protective LIMIT ≥ ${fmtNum(g.latency_warn_ms, 0)} ms · BLOCK ≥ ${fmtNum(g.latency_block_ms, 0)} ms`}
            level={g.broker_latency_ms == null ? undefined : g.broker_latency_ms >= g.latency_block_ms ? 'BREACH' : g.broker_latency_ms >= g.latency_warn_ms ? 'WARNING' : 'SAFE'} />
          <Row label="Recent avg slippage" value={g.recent_slippage_bps != null ? `${fmtNum(g.recent_slippage_bps, 1)} bps` : 'n/a (needs 3 fills)'}
            note={`protective LIMIT > ${fmtNum(g.slippage_warn_bps, 0)} bps`} level={g.recent_slippage_bps == null ? undefined : g.recent_slippage_bps > g.slippage_warn_bps ? 'WARNING' : 'SAFE'} />
          <div className="pt-1 text-muted">1m volatility (stdev of 20 log returns) · protective LIMIT &gt; {fmtNum(g.max_volatility_bps, 0)} bps</div>
          <div className="flex flex-wrap gap-3">{Object.entries(g.volatility_bps).map(([s, v]: any) => <span key={s} className="num">{s} <span className={v != null && v > g.max_volatility_bps ? 'text-warn' : ''}>{v != null ? `${fmtNum(v, 1)} bps` : 'n/a'}</span></span>)}</div>
        </Panel>
      </div>
      <div className="grid gap-3 lg:grid-cols-3">
        {Object.entries(d.strategies).map(([sid, s]: any) => (
          <Panel key={sid} title={<Link to={`/strategies/${sid}`} className="hover:text-text">{s.name}</Link>} right={<StatusBadge status={s.status} />} bodyClass="p-3 space-y-3">
            {s.status_reason && <div className="text-xs text-amber-400">{s.status_reason}</div>}
            {s.meters.map((m: any) => <Meter key={m.label} m={m} />)}
          </Panel>
        ))}
      </div>
      <Limits limits={d.limits} admin={user?.role === 'ADMIN'} onSaved={risk.reload} />
      <Panel title="Recent risk decisions" right={<select className="input" value={decision} onChange={(e) => setDecision(e.target.value)}><option value="">All</option><option value="APPROVED">Approved</option><option value="REJECTED">Rejected</option></select>}>
        {!events.data ? <Loading /> : events.data.length === 0 ? <Empty>No risk decisions yet</Empty> : (
          <div className="overflow-x-auto"><table className="tbl"><thead><tr><th>Time</th><th>Decision</th><th>Strategy</th><th>Rule</th><th>Actual</th><th>Allowed</th><th>Message</th><th></th></tr></thead>
            <tbody>{events.data.map((r) => (
              <Fragment key={r.id}>
                <tr className="clickable" onClick={() => setOpen(open === r.id ? null : r.id)}>
                  <td className="num text-muted">{fmtTime(r.created_at)}</td><td><StatusBadge status={r.decision} /></td><td>{r.strategy_id}</td>
                  <td className="font-mono text-[11px]">{r.rule || '—'}</td><td className="num">{r.actual || '—'}</td><td className="num">{r.allowed || '—'}</td>
                  <td className="max-w-[420px] truncate" title={r.message}>{r.message}</td>
                  <td>{r.correlation_id && <Link className="text-info" to={`/audit?trace=${r.correlation_id}`} onClick={(e) => e.stopPropagation()}>trace</Link>}</td>
                </tr>
                {open === r.id && <tr><td colSpan={8} className="bg-bg p-2 whitespace-normal"><RiskChecks checks={r.checks} /></td></tr>}
              </Fragment>))}</tbody></table></div>
        )}
      </Panel>
    </div>
  )
}

function Row({ label, value, note, level }: { label: string; value: string; note: string; level?: string }) {
  return <div className="flex flex-wrap items-center justify-between gap-2"><span className="text-muted">{label}</span><span className="flex items-center gap-2"><span className="num">{value}</span>{level && <StatusBadge status={level} />}</span><span className="w-full text-[11px] text-muted">{note}</span></div>
}

function Limits({ limits, admin, onSaved }: { limits: any; admin: boolean; onSaved: () => void }) {
  const [edit, setEdit] = useState<Record<string, string>>({})
  const [err, setErr] = useState<string | null>(null)
  const save = async (scope: string, strategy_id: string | null, name: string, key: string) => {
    setErr(null)
    try { await api.put('/risk/limits', { scope, strategy_id, name, value: Number(edit[key]) }); setEdit((e) => { const n = { ...e }; delete n[key]; return n }); onSaved() } catch (e: any) { setErr(e.message) }
  }
  const cell = (scope: string, sid: string | null, l: any) => {
    const key = `${scope}:${sid}:${l.name}`
    if (!admin) return <span className="num">{fmtNum(l.value)}</span>
    return key in edit ? (
      <span className="flex items-center gap-1"><input className="input w-28 num" type="number" min={0} value={edit[key]} onChange={(e) => setEdit({ ...edit, [key]: e.target.value })} />
        <button className="btn" onClick={() => save(scope, sid, l.name, key)}>Save</button><button className="btn" onClick={() => setEdit((e) => { const n = { ...e }; delete n[key]; return n })}>×</button></span>
    ) : <button className="num underline decoration-dotted hover:text-white" onClick={() => setEdit({ ...edit, [key]: String(l.value) })}>{fmtNum(l.value)}</button>
  }
  return (
    <Panel title={`Risk limits${admin ? ' (click a value to edit — ADMIN)' : ''}`} bodyClass="grid gap-3 p-3 lg:grid-cols-2">
      <ErrorText error={err} />
      <table className="tbl"><thead><tr><th>Global limit</th><th>Value</th></tr></thead>
        <tbody>{limits.global.map((l: any) => <tr key={l.name}><td>{l.label}</td><td>{cell('GLOBAL', null, l)}</td></tr>)}</tbody></table>
      <table className="tbl"><thead><tr><th>Strategy</th><th>Limit</th><th>Value</th></tr></thead>
        <tbody>{Object.entries(limits.strategy).flatMap(([sid, ls]: any) => ls.map((l: any) => <tr key={sid + l.name}><td>{sid}</td><td>{l.label}</td><td>{cell('STRATEGY', sid, l)}</td></tr>))}</tbody></table>
    </Panel>
  )
}
