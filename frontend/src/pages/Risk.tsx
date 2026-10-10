import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../hooks/auth'
import { useApi } from '../hooks/useEvents'
import { api } from '../services/api'
import { fmtNum } from '../services/format'
import RiskAdvisor from '../components/RiskAdvisor'
import { ErrorText, Loading, Meter, Panel, StatusBadge } from '../components/ui'

export default function Risk() {
  const { user } = useAuth()
  const risk = useApi<any>('/risk', ['risk.decision', 'pnl.update', 'strategy.status'], 2000)
  const d = risk.data
  if (!d) return risk.error ? <ErrorText error={risk.error} /> : <Loading />
  const g = d.guard
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3"><h1 className="text-base font-semibold">Risk Firewall &amp; Virtual Margin</h1>
        <span className="text-xs text-muted num">{d.stats.approved} approved · {d.stats.rejected} rejected (since start)</span></div>
      <RiskAdvisor currentLimits={d.limits} isAdmin={user?.role === 'ADMIN'} onLimitsApplied={risk.reload} />
      <div className="grid gap-3 lg:grid-cols-2">
        <Panel title="Portfolio utilization" bodyClass="p-3 space-y-3">
          {d.portfolio.map((m: any) => <Meter key={m.label} m={m} />)}
          {user?.role === 'ADMIN' && <ManualLimits limits={d.limits} names={Object.fromEntries(Object.entries(d.strategies).map(([k, s]: any) => [k, s.name]))} onSaved={risk.reload} />}
        </Panel>
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
    </div>
  )
}

/** Set the problem statement's three limits by hand (ADMIN): account max daily loss and max orders per minute,
 *  max position size for every strategy or one of them. */
function ManualLimits({ limits, names, onSaved }: { limits: any; names: Record<string, string>; onSaved: () => void }) {
  const g = (n: string) => limits.global.find((x: any) => x.name === n)?.value
  const sids = Object.keys(limits.strategy)
  const [open, setOpen] = useState(false)
  const [loss, setLoss] = useState('')
  const [opm, setOpm] = useState('')
  const [pos, setPos] = useState('')
  const [target, setTarget] = useState('ALL')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [ok, setOk] = useState<string | null>(null)
  const posOf = (sid: string) => limits.strategy[sid]?.find((x: any) => x.name === 'max_position')?.value
  const start = () => {
    setLoss(String(g('max_daily_loss') ?? '')); setOpm(String(g('max_orders_per_min') ?? ''))
    setPos(String(posOf(target === 'ALL' ? sids[0] : target) ?? '')); setErr(null); setOk(null); setOpen(true)
  }
  const save = async () => {
    setBusy(true); setErr(null); setOk(null)
    try {
      const put = (scope: string, name: string, value: string, strategy_id: string | null = null) =>
        api.put('/risk/limits', { scope, strategy_id, name, value: Number(value) })
      if (loss !== '' && Number(loss) !== Number(g('max_daily_loss'))) await put('GLOBAL', 'max_daily_loss', loss)
      if (opm !== '' && Number(opm) !== Number(g('max_orders_per_min'))) await put('GLOBAL', 'max_orders_per_min', opm)
      if (pos !== '') for (const sid of (target === 'ALL' ? sids : [target])) if (Number(pos) !== Number(posOf(sid))) await put('STRATEGY', 'max_position', pos, sid)
      setOk('Limits saved - the Risk Firewall enforces them on the next order'); setOpen(false); onSaved()
    } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  if (!open) return (
    <div className="flex items-center gap-2 border-t border-line pt-3">
      <button className="btn" onClick={start}>Set limits manually</button>{ok && <span className="text-xs text-green-400">{ok}</span>}
    </div>
  )
  return (
    <div className="space-y-2 border-t border-line pt-3 text-xs">
      <div className="grid gap-2 sm:grid-cols-3">
        <label className="text-muted">Max daily loss (₹, account)
          <input className="input mt-1 w-full num" type="number" min={1} step={100} value={loss} onChange={(e) => setLoss(e.target.value)} /></label>
        <label className="text-muted">Max position size (shares)
          <input className="input mt-1 w-full num" type="number" min={1} step={1} value={pos} onChange={(e) => setPos(e.target.value)} /></label>
        <label className="text-muted">Max orders per minute (account)
          <input className="input mt-1 w-full num" type="number" min={1} max={600} step={1} value={opm} onChange={(e) => setOpm(e.target.value)} /></label>
      </div>
      <label className="block text-muted">Max position applies to
        <select className="input mt-1 w-full" value={target} onChange={(e) => { setTarget(e.target.value); const v = posOf(e.target.value === 'ALL' ? sids[0] : e.target.value); if (v != null) setPos(String(v)) }}>
          <option value="ALL">All strategies ({sids.length})</option>
          {sids.map((s) => <option key={s} value={s}>{names[s] ? `${names[s]} · ` : ''}{s}</option>)}
        </select></label>
      <div className="text-muted">Per-strategy max daily loss and orders per minute are set on each strategy (Strategies → Configure, or the cards below).</div>
      <ErrorText error={err} />
      <div className="flex gap-2"><button className="btn btn-primary" disabled={busy} onClick={save}>Save limits</button><button className="btn" onClick={() => setOpen(false)}>Cancel</button></div>
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
  // per strategy: one row, one column per limit (the problem statement's three first)
  const COLS: [string, string][] = [['max_daily_loss', 'Max loss / day (₹)'], ['max_position', 'Max position (shares)'],
    ['max_orders_per_min', 'Max orders / min'], ['max_exposure', 'Max exposure (₹)']]
  const sids = Object.keys(limits.strategy)
  return (
    <Panel title={`Risk limits${admin ? ' · click a value to edit' : ''}`} bodyClass="space-y-4 p-3">
      <ErrorText error={err} />
      <div>
        <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted">Per strategy</div>
        <div className="overflow-x-auto"><table className="tbl">
          <thead><tr><th>Strategy</th>{COLS.map(([, l]) => <th key={l} className="text-right">{l}</th>)}</tr></thead>
          <tbody>{sids.map((sid) => {
            const byName = Object.fromEntries((limits.strategy[sid] as any[]).map((l) => [l.name, l]))
            return (
              <tr key={sid}><td className="font-medium whitespace-nowrap">{sid}</td>
                {COLS.map(([n]) => <td key={n} className="text-right">{byName[n] ? <span className="inline-flex justify-end">{cell('STRATEGY', sid, byName[n])}</span> : '—'}</td>)}</tr>
            )
          })}</tbody>
        </table></div>
      </div>
      <div>
        <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted">Whole account</div>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {limits.global.map((l: any) => (
            <div key={l.name} className="flex items-center justify-between gap-3 rounded border border-line bg-bg px-3 py-2 text-xs">
              <span className="text-muted">{l.label}</span>{cell('GLOBAL', null, l)}
            </div>
          ))}
        </div>
      </div>
    </Panel>
  )
}
