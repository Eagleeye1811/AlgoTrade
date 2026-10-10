import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Plus } from 'lucide-react'
import { useApi } from '../hooks/useEvents'
import { useAuth } from '../hooks/auth'
import { api } from '../services/api'
import { fmtINR, fmtNum, fmtQty, fmtSigned, pnlClass } from '../services/format'
import StrategyConfigModal from '../components/StrategyConfigModal'
import SubscribeModal from '../components/SubscribeModal'
import StrategyChartModal from '../components/StrategyChartModal'
import { LineChart } from 'lucide-react'
import { Empty, ErrorText, JsonKV, Loading, Panel, Side, StatusBadge } from '../components/ui'

export default function Strategies() {
  const { user } = useAuth()
  const [scope, setScope] = useState<'mine' | 'all'>('mine')
  const { data, error, reload } = useApi<any[]>(`/strategies${scope === 'mine' ? '?mine=true' : ''}`,
    ['strategy.update', 'strategy.status', 'strategy.config', 'position.update', 'pnl.update', 'strategy.signal'], 1500)
  const [cfg, setCfg] = useState<any>(null)
  const [sub, setSub] = useState(false)
  const [chartOf, setChartOf] = useState<any>(null)
  const [err, setErr] = useState<string | null>(null)
  const act = async (fn: () => Promise<unknown>) => { setErr(null); try { await fn(); reload() } catch (e: any) { setErr(e.message) } }
  const canManage = (s: any) => user?.role === 'ADMIN' || s.user_id === user?.id

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-base font-semibold">Strategies</h1>
        <div className="flex overflow-hidden rounded-md border border-line bg-bg p-0.5 text-xs">
          {(['mine', 'all'] as const).map((k) => (
            <button key={k} onClick={() => setScope(k)} className={`rounded px-3 py-1 font-medium ${scope === k ? 'bg-info text-white' : 'text-muted hover:text-text'}`}>
              {k === 'mine' ? 'My subscriptions' : 'All on account'}</button>
          ))}
        </div>
        <button className="btn btn-primary ml-auto" onClick={() => setSub(true)}><Plus size={14} /> Subscribe</button>
      </div>
      <ErrorText error={err ?? error} />
      {!data ? <Loading /> : data.length === 0 ? (
        <Panel><Empty>{scope === 'mine' ? 'You have no subscriptions yet — click Subscribe to pick a strategy and a stock.' : 'No strategies are running on the account.'}</Empty></Panel>
      ) : (
        <div className="grid gap-3 lg:grid-cols-3">
          {data.map((s) => (
            <Panel key={s.id} title={<span className="text-text normal-case text-sm tracking-normal">{s.name} <span className="text-muted">· {s.symbol} · {s.timeframe}</span></span>}
              right={<StatusBadge status={s.status} />} bodyClass="p-3 space-y-3 text-xs">
              {s.status_reason && <div className="text-amber-400">{s.status_reason}</div>}
              {Number(s.allocation) <= 0 && <div className="rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1.5 text-amber-300">
                No capital allocated — this strategy cannot open positions. {canManage(s) && <button className="underline" onClick={() => setCfg(s)}>Set allocation</button>}</div>}
              <div>
                <div className="flex items-center gap-2"><span className="text-muted">Current signal</span><StatusBadge status={s.current_signal.signal} /></div>
                <div className="mt-1 text-muted">{s.current_signal.reason}</div>
              </div>
              <div className="grid grid-cols-2 gap-x-3 gap-y-1.5">
                <span className="text-muted">Position</span><span className="num text-right"><Side side={s.position.side} /> {fmtQty(s.position.quantity)} @ {Number(s.position.quantity) ? fmtNum(s.position.avg_price) : '—'}</span>
                <span className="text-muted">LTP</span><span className="num text-right">{fmtNum(s.ltp)}</span>
                <span className="text-muted">Stop-loss / target</span><span className="num text-right"><span className="text-neg">{fmtNum(s.exits?.stop_loss)}</span> / <span className="text-pos">{fmtNum(s.exits?.take_profit)}</span></span>
                <span className="text-muted">P&amp;L today</span><span className={`num text-right ${pnlClass(s.pnl?.today_pnl)}`}>{fmtSigned(s.pnl?.today_pnl)}</span>
                <span className="text-muted">Realized / unrealized</span><span className="num text-right"><span className={pnlClass(s.pnl?.realized)}>{fmtSigned(s.pnl?.realized)}</span> / <span className={pnlClass(s.pnl?.unrealized)}>{fmtSigned(s.pnl?.unrealized)}</span></span>
                <span className="text-muted">Margin / allocation</span><span className="num text-right">{fmtINR(s.pnl?.margin_used)} / {fmtINR(s.allocation)}</span>
                <span className="text-muted">Order size</span><span className="num text-right">{fmtQty(s.quantity)} sh</span>
              </div>
              <div className="grid grid-cols-3 gap-2 rounded border border-line p-2 text-center">
                <div><div className="text-[10px] uppercase text-muted">Max loss/day</div><div className="num">{fmtINR(s.limits?.max_daily_loss)}</div></div>
                <div><div className="text-[10px] uppercase text-muted">Max position</div><div className="num">{fmtQty(s.limits?.max_position)}</div></div>
                <div><div className="text-[10px] uppercase text-muted">Orders/min</div><div className="num">{fmtQty(s.limits?.max_orders_per_min)}</div></div>
              </div>
              <div><div className="mb-0.5 text-muted">Parameters (v{s.config_version})</div><JsonKV data={s.params} /></div>
              <div className="flex flex-wrap gap-2 pt-1">
                <Link className="btn btn-primary" to={`/strategies/${s.id}`}>Details</Link>
                <button className="btn" onClick={() => setChartOf(s)}><LineChart size={13} /> Chart</button>
                {canManage(s) && <>
                  <button className="btn" onClick={() => act(() => api.post(`/strategies/${s.id}/${s.status === 'RUNNING' ? 'pause' : 'resume'}`))}>{s.status === 'RUNNING' ? 'Pause' : 'Resume'}</button>
                  <button className="btn" onClick={() => setCfg(s)}>Configure</button>
                  <button className="btn" title="Only when flat with no working orders" onClick={() => act(() => api.del(`/strategies/${s.id}`))}>Unsubscribe</button>
                </>}
              </div>
            </Panel>
          ))}
        </div>
      )}
      <StrategyConfigModal strategy={cfg} open={!!cfg} onClose={() => setCfg(null)} onSaved={reload} />
      <StrategyChartModal strategy={chartOf} onClose={() => setChartOf(null)} />
      <SubscribeModal open={sub} onClose={() => setSub(false)} onDone={() => { setScope('mine'); reload() }} />
    </div>
  )
}
