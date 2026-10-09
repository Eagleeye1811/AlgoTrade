import { useState } from 'react'
import { useAuth } from '../hooks/auth'
import { useSystem } from '../hooks/system'
import { useApi } from '../hooks/useEvents'
import { api } from '../services/api'
import { fmtINR, fmtMarketTime, fmtTime } from '../services/format'
import { Badge, ErrorText, Loading, Panel, StatusBadge } from '../components/ui'

export default function Settings() {
  const { user } = useAuth()
  const { reload } = useSystem()
  const acct = useApi<any>('/account', ['trading.status', 'market.status', 'broker.status', 'pnl.update'], 3000)
  const admin = user?.role === 'ADMIN'
  const users = useApi<any[]>(admin ? '/auth/users' : null)
  const [speed, setSpeed] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const act = async (fn: () => Promise<any>, ok: string) => {
    setBusy(true); setErr(null); setMsg(null)
    try { const r = await fn(); setMsg(typeof r === 'string' ? r : ok); acct.reload(); reload() } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  const d = acct.data
  if (!d) return acct.error ? <ErrorText error={acct.error} /> : <Loading />
  const a = d.account, p = d.portfolio, m = d.market
  return (
    <div className="space-y-3">
      <h1 className="text-base font-semibold">Settings / Account</h1>
      <div className="rounded border border-violet-500/30 bg-violet-500/10 p-3 text-xs text-violet-200"><b>{d.execution_mode}.</b> {d.broker_note}</div>
      <ErrorText error={err} />{msg && <div className="text-xs text-green-400">{msg}</div>}
      <div className="grid gap-3 lg:grid-cols-2">
        <Panel title="Trading account" bodyClass="p-3 text-xs">
          {!a ? <div>Not configured</div> : (
            <div className="grid grid-cols-2 gap-y-1.5">
              <span className="text-muted">Name</span><span>{a.name}</span>
              <span className="text-muted">Account type</span><span><Badge tone="purple">{a.account_type}</Badge></span>
              <span className="text-muted">Broker</span><span>{a.broker} · {a.broker_account_id}</span>
              <span className="text-muted">Starting capital</span><span className="num">{fmtINR(a.starting_capital)}</span>
              <span className="text-muted">Available capital</span><span className="num">{fmtINR(p.available_capital)}</span>
              <span className="text-muted">Used margin</span><span className="num">{fmtINR(p.margin_used)}</span>
              <span className="text-muted">Exposure</span><span className="num">{fmtINR(p.exposure)}</span>
              <span className="text-muted">Broker connection</span><span><StatusBadge status={d.broker.available ? 'CONNECTED' : 'UNAVAILABLE'} /> {d.broker.current_latency_ms != null && <span className="num text-muted">{Math.round(d.broker.current_latency_ms)} ms</span>}</span>
              <span className="text-muted">Market data</span><span><StatusBadge status={m.display_status} /></span>
              <span className="text-muted">Trading status</span><span><StatusBadge status={d.trading.status} /> <span className="text-muted">{d.trading.reason}</span></span>
              <span className="text-muted">Created</span><span className="num">{fmtTime(a.created_at, true)}</span>
            </div>
          )}
          <div className="mt-3 text-muted">Allocations</div>
          {Object.entries(d.allocations).map(([k, v]: any) => <div key={k} className="flex justify-between"><span>{v.name}</span><span className="num">{fmtINR(v.allocation)}</span></div>)}
        </Panel>
        <Panel title="Market data" bodyClass="p-3 text-xs space-y-2">
          <div className="grid grid-cols-2 gap-y-1.5">
            <span className="text-muted">Mode</span><span>{m.mode}</span>
            <span className="text-muted">Source</span><span>{m.source}</span>
            <span className="text-muted">Status</span><span><StatusBadge status={m.display_status} /> <span className="text-muted">{m.detail}</span></span>
            <span className="text-muted">NSE session open now</span><span>{m.market_open ? 'yes' : 'no'}</span>
            <span className="text-muted">Auto session switching</span>
            <span>{m.auto_switching
              ? <>on — LIVE 09:15–15:30 IST (Mon–Fri), REPLAY otherwise{m.override ? <span className="text-warn"> · operator override: {m.override} until session change</span> : null}</>
              : 'off (mode pinned in backend config)'}</span>
            {m.last_switch && <>
              <span className="text-muted">Last switch</span>
              <span>{m.last_switch.from} → {m.last_switch.to} ({m.last_switch.reason}); {m.last_switch.cancelled_orders.length} order(s) cancelled
                {m.last_switch.carried_positions.length ? <span className="text-warn"> · positions now marked at {m.last_switch.to} prices: {m.last_switch.carried_positions.join(', ')}</span> : null}</span>
            </>}
            <span className="text-muted">Symbols</span><span>{(m.symbols || []).join(', ')}</span>
            {m.mode === 'REPLAY' && <>
              <span className="text-muted">Replay position</span><span className="num">{m.cursor} / {m.total_minutes} min · {fmtMarketTime(m.market_time)}{m.ended ? ' · ENDED' : ''}</span>
              <span className="text-muted">Speed</span><span className="num">{m.speed}x</span>
            </>}
            <span className="text-muted">Stale threshold</span><span className="num">{m.stale_after_s}s</span>
            <span className="text-muted">Malformed bars dropped</span><span className="num">{m.dropped_malformed_bars}</span>
          </div>
          {admin && m.mode === 'REPLAY' && (
            <div className="flex flex-wrap items-center gap-2 border-t border-line pt-2">
              <input className="input w-24 num" type="number" min={1} max={600} placeholder={String(m.speed)} value={speed} onChange={(e) => setSpeed(e.target.value)} />
              <button className="btn" disabled={busy || !speed} onClick={() => act(() => api.post('/market/replay', { speed: Number(speed) }), `Replay speed set to ${speed}x`)}>Set speed</button>
              <button className="btn" disabled={busy} onClick={() => act(() => api.post('/market/replay', { restart: true }), 'Replay restarted from the beginning')}>Restart replay</button>
              <button className="btn" disabled={busy} onClick={() => act(async () => { const r = await api.post('/market/refresh-replay-data'); return 'Fetched: ' + Object.entries(r).map(([k, v]: any) => `${k} ${v.bars} bars`).join(', ') + ' (applies on replay restart)' }, '')}>Refresh replay data from API</button>
            </div>
          )}
        </Panel>
      </div>
      {admin && (
        <Panel title="Users & roles">
          {!users.data ? <Loading /> : (
            <table className="tbl"><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Created</th><th></th></tr></thead>
              <tbody>{users.data.map((u) => (
                <tr key={u.id}><td>{u.name}</td><td>{u.email}</td><td><Badge tone={u.role === 'ADMIN' ? 'purple' : 'blue'}>{u.role}</Badge></td><td className="num text-muted">{fmtTime(u.created_at, true)}</td>
                  <td>{u.id !== user?.id && <button className="btn" disabled={busy} onClick={() => act(async () => { await api.put(`/auth/users/${u.id}/role`, { role: u.role === 'ADMIN' ? 'TRADER' : 'ADMIN' }); users.reload() }, 'Role updated')}>Make {u.role === 'ADMIN' ? 'TRADER' : 'ADMIN'}</button>}</td></tr>))}</tbody></table>
          )}
        </Panel>
      )}
    </div>
  )
}
