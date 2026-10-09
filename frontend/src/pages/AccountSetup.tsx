import { FormEvent, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../hooks/auth'
import { useSystem } from '../hooks/system'
import { api } from '../services/api'
import { fmtINR } from '../services/format'
import { ErrorText, Panel } from '../components/ui'

export default function AccountSetup() {
  const { user, logout } = useAuth()
  const { overview, reload } = useSystem()
  const nav = useNavigate()
  const [name, setName] = useState('Paper Trading Account')
  const [capital, setCapital] = useState('1000000')
  const [strats, setStrats] = useState<{ id: string; name: string }[]>([])
  const [alloc, setAlloc] = useState<Record<string, string>>({})
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => { if (overview?.account_configured) nav('/') }, [overview, nav])
  useEffect(() => { api.get('/strategies').then((s) => setStrats(s.map((x: any) => ({ id: x.id, name: x.name })))).catch(() => {}) }, [])
  const total = Object.values(alloc).reduce((a, v) => a + (Number(v) || 0), 0)
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setErr(null)
    const allocations = Object.fromEntries(Object.entries(alloc).filter(([, v]) => v !== '').map(([k, v]) => [k, Number(v)]))
    try {
      await api.post('/account/setup', { name, starting_capital: Number(capital), allocations: Object.keys(allocations).length === strats.length ? allocations : null })
      reload(); nav('/')
    } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <Panel title="Account setup" className="w-full max-w-lg" bodyClass="p-5 space-y-3">
        <div className="rounded border border-violet-500/30 bg-violet-500/10 p-3 text-xs text-violet-200">
          PAPER TRADING · SIMULATED EXECUTION. No verified 021 broker API or credentials are configured, so orders execute on the AlgoTrade Paper Broker Simulator. No real money is involved.
        </div>
        {user?.role !== 'ADMIN' ? (
          <div className="space-y-3 text-sm"><p>No trading account is configured yet. Ask an ADMIN to complete account setup.</p>
            <button className="btn" onClick={async () => { await logout(); nav('/login') }}>Logout</button></div>
        ) : (
          <form onSubmit={submit} className="space-y-3">
            <label className="block text-xs text-muted">Account name<input className="input mt-1 w-full" value={name} onChange={(e) => setName(e.target.value)} required /></label>
            <label className="block text-xs text-muted">Starting capital (INR)<input className="input mt-1 w-full num" type="number" min={100000} step={1000} value={capital} onChange={(e) => setCapital(e.target.value)} required /></label>
            <div className="text-xs text-muted">Strategy allocations (virtual margin) — leave all empty for the default 40% / 30% / 30% split.</div>
            {strats.map((s) => (
              <label key={s.id} className="flex items-center justify-between gap-3 text-xs"><span>{s.name}</span>
                <input className="input w-40 num" type="number" min={0} value={alloc[s.id] ?? ''} placeholder="default" onChange={(e) => setAlloc({ ...alloc, [s.id]: e.target.value })} /></label>
            ))}
            {total > 0 && <div className={`text-xs num ${total > Number(capital) ? 'text-red-400' : 'text-muted'}`}>Allocated {fmtINR(total)} of {fmtINR(capital)}</div>}
            <ErrorText error={err} />
            <button className="btn btn-primary" disabled={busy}>Create paper account</button>
          </form>
        )}
      </Panel>
    </div>
  )
}
