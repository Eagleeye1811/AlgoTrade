import { useState } from 'react'
import { useAuth } from '../hooks/auth'
import { useSystem } from '../hooks/system'
import { useApi } from '../hooks/useEvents'
import { api } from '../services/api'
import { fmtINR, fmtTime } from '../services/format'
import { Badge, ErrorText, Loading, Panel, StatusBadge } from '../components/ui'
import TelegramPanel from '../components/TelegramPanel'

export default function Settings() {
  const { user } = useAuth()
  const { reload } = useSystem()
  const acct = useApi<any>('/account', ['trading.status', 'market.status', 'broker.status', 'pnl.update'], 3000)
  const admin = user?.role === 'ADMIN'
  const users = useApi<any[]>(admin ? '/auth/users' : null)
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
      <TelegramPanel />
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
            <span className="text-muted">Symbols</span><span>{(m.symbols || []).join(', ')}</span>
            <span className="text-muted">Frames received</span><span className="num">{m.frames ?? '—'}</span>
            <span className="text-muted">Stale threshold</span><span className="num">{m.stale_after_s}s</span>
            <span className="text-muted">Malformed frames dropped</span><span className="num">{m.dropped_malformed_bars}</span>
          </div>
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
