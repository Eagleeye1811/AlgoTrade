import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useApi } from '../hooks/useEvents'
import { api } from '../services/api'
import { fmtINR, fmtNum, fmtQty, fmtSigned, pnlClass } from '../services/format'
import StrategyConfigModal from '../components/StrategyConfigModal'
import { ErrorText, JsonKV, Loading, Panel, Side, StatusBadge } from '../components/ui'

export default function Strategies() {
  const { data, error, reload } = useApi<any[]>('/strategies', ['strategy.update', 'strategy.status', 'strategy.config', 'position.update', 'pnl.update', 'strategy.signal'], 1500)
  const [cfg, setCfg] = useState<any>(null)
  const [err, setErr] = useState<string | null>(null)
  const toggle = async (s: any) => {
    setErr(null)
    try { await api.post(`/strategies/${s.id}/${s.status === 'RUNNING' ? 'pause' : 'resume'}`); reload() } catch (e: any) { setErr(e.message) }
  }
  if (!data) return error ? <ErrorText error={error} /> : <Loading />
  return (
    <div className="space-y-3">
      <h1 className="text-base font-semibold">Strategy management</h1>
      <ErrorText error={err} />
      <div className="grid gap-3 lg:grid-cols-3">
        {data.map((s) => (
          <Panel key={s.id} title={<span className="text-text normal-case text-sm tracking-normal">{s.name} <span className="text-muted">· {s.symbol} · {s.timeframe}</span></span>} right={<StatusBadge status={s.status} />} bodyClass="p-3 space-y-3 text-xs">
            {s.status_reason && <div className="text-amber-400">{s.status_reason}</div>}
            <div>
              <div className="flex items-center gap-2"><span className="text-muted">Current signal</span><StatusBadge status={s.current_signal.signal} /></div>
              <div className="mt-1 text-muted">{s.current_signal.reason}</div>
            </div>
            <div className="grid grid-cols-2 gap-x-3 gap-y-1.5">
              <span className="text-muted">Position</span><span className="num text-right"><Side side={s.position.side} /> {fmtQty(s.position.quantity)} @ {s.position.quantity ? fmtNum(s.position.avg_price) : '—'}</span>
              <span className="text-muted">LTP</span><span className="num text-right">{fmtNum(s.ltp)}</span>
              <span className="text-muted">Realized P&amp;L</span><span className={`num text-right ${pnlClass(s.pnl?.realized)}`}>{fmtSigned(s.pnl?.realized)}</span>
              <span className="text-muted">Unrealized P&amp;L</span><span className={`num text-right ${pnlClass(s.pnl?.unrealized)}`}>{fmtSigned(s.pnl?.unrealized)}</span>
              <span className="text-muted">Exposure</span><span className="num text-right">{fmtINR(s.pnl?.exposure)}</span>
              <span className="text-muted">Margin / allocation</span><span className="num text-right">{fmtINR(s.pnl?.margin_used)} / {fmtINR(s.allocation)}</span>
              <span className="text-muted">Order size</span><span className="num text-right">{fmtQty(s.quantity)} sh</span>
            </div>
            <div><div className="mb-0.5 text-muted">Parameters (v{s.config_version})</div><JsonKV data={s.params} /></div>
            <div className="flex flex-wrap gap-2 pt-1">
              <Link className="btn btn-primary" to={`/strategies/${s.id}`}>View details</Link>
              <button className="btn" onClick={() => toggle(s)}>{s.status === 'RUNNING' ? 'Pause' : 'Resume'}</button>
              <button className="btn" onClick={() => setCfg(s)}>Configure</button>
            </div>
          </Panel>
        ))}
      </div>
      <StrategyConfigModal strategy={cfg} open={!!cfg} onClose={() => setCfg(null)} onSaved={reload} />
    </div>
  )
}
