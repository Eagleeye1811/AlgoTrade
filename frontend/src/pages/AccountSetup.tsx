import { FormEvent, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../hooks/auth'
import { useSystem } from '../hooks/system'
import { api } from '../services/api'
import { ErrorText, Panel } from '../components/ui'

export default function AccountSetup() {
  const { user, logout } = useAuth()
  const { overview, reload } = useSystem()
  const nav = useNavigate()
  const [name, setName] = useState('021 Sandbox Account')
  const [capital, setCapital] = useState('1000000')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => { if (overview?.account_configured) nav('/') }, [overview, nav])
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setErr(null)
    try {
      await api.post('/account/setup', { name, starting_capital: Number(capital) })
      reload(); nav('/')
    } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <Panel title="Account setup" className="w-full max-w-lg" bodyClass="p-5 space-y-3">
        <div className="rounded border border-violet-500/30 bg-violet-500/10 p-3 text-xs text-violet-200">
          Orders go to the 021 developer sandbox (POST /orders) with the UCC configured on the server; the account has ₹10,00,000 of virtual funds per day on a simulated exchange. Capital here is what the platform allocates to strategies (max ₹10,00,000). No real money is involved.
        </div>
        {user?.role !== 'ADMIN' ? (
          <div className="space-y-3 text-sm"><p>No trading account is configured yet. Ask an ADMIN to complete account setup.</p>
            <button className="btn" onClick={async () => { await logout(); nav('/login') }}>Logout</button></div>
        ) : (
          <form onSubmit={submit} className="space-y-3">
            <label className="block text-xs text-muted">Account name<input className="input mt-1 w-full" value={name} onChange={(e) => setName(e.target.value)} required /></label>
            <label className="block text-xs text-muted">Starting capital (INR)<input className="input mt-1 w-full num" type="number" min={100000} step={1000} value={capital} onChange={(e) => setCapital(e.target.value)} required /></label>
            <div className="text-xs text-muted">No capital is allocated automatically: after setup, give each subscribed strategy its allocation in Strategies → Configure (a strategy with no allocation cannot open positions).</div>
            <ErrorText error={err} />
            <button className="btn btn-primary" disabled={busy}>Configure account</button>
          </form>
        )}
      </Panel>
    </div>
  )
}
