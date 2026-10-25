import { FormEvent, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Sparkles, UserCheck, TrendingUp, ShieldAlert } from 'lucide-react'
import { useAuth } from '../hooks/auth'
import { useSystem } from '../hooks/system'
import { api } from '../services/api'
import { ErrorText, Panel } from '../components/ui'

export default function AccountSetup() {
  const { user, logout } = useAuth()
  const { overview, reload } = useSystem()
  const nav = useNavigate()
  const [name, setName] = useState('Trading Account (Vedant Gawali)')
  const [capital, setCapital] = useState('1000000')
  const [profile, setProfile] = useState<'vedant' | 'salaried' | 'senior'>('vedant')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => { if (overview?.account_configured) nav('/') }, [overview, nav])

  const selectPreset = (p: 'vedant' | 'salaried' | 'senior') => {
    setProfile(p)
    if (p === 'vedant') {
      setName('Trading Account (Vedant Gawali)')
      setCapital('1000000')
    } else if (p === 'salaried') {
      setName('Primary Trading Account (Salaried 30yr)')
      setCapital('1000000')
    } else {
      setName('Conservative Portfolio (Retiree 60yr)')
      setCapital('1000000')
    }
  }

  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setErr(null)
    try {
      let allocations: Record<string, number> = {
        time_entry: 200000,
        open_breakout: 300000,
        ema_trend: 300000,
      }
      if (profile === 'senior') {
        allocations = {
          time_entry: 100000,
          open_breakout: 150000,
          ema_trend: 150000,
        }
      }
      await api.post('/account/setup', { name, starting_capital: Number(capital), allocations })
      
      // Auto-apply profile risk limits for selected preset
      const age = profile === 'vedant' ? 21 : profile === 'salaried' ? 30 : 60
      const income = profile === 'vedant' ? 0 : profile === 'salaried' ? 100000 : 40000
      const appetite = profile === 'vedant' ? 'AGGRESSIVE' : profile === 'salaried' ? 'MODERATE' : 'CONSERVATIVE'
      
      try {
        await api.post('/risk/apply-recommendations', { age, income, risk_appetite: appetite })
      } catch (_) {}

      reload(); nav('/')
    } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <Panel title="Account setup &amp; Risk Profiling" className="w-full max-w-lg" bodyClass="p-5 space-y-4">
        <div className="rounded border border-violet-500/30 bg-violet-500/10 p-3 text-xs text-violet-200">
          {overview?.broker?.paper
            ? <>Orders are filled by the local paper-trading simulator against the market feed. Capital here is the virtual money the platform allocates to strategies. No real orders are placed and no real money is involved.</>
            : <>Orders go to the 021 developer sandbox (POST /orders) with the UCC configured on the server; the account has ₹10,00,000 of virtual funds per day on a simulated exchange. Capital here is what the platform allocates to strategies (max ₹10,00,000). No real money is involved.</>}
        </div>

        {/* Profile Presets */}
        <div className="space-y-1.5">
          <div className="text-xs font-semibold text-muted flex items-center gap-1.5">
            <Sparkles size={13} className="text-amber-400" />
            AI Risk Profile Presets:
          </div>
          <div className="grid grid-cols-3 gap-2 text-xs">
            <button
              type="button"
              onClick={() => selectPreset('vedant')}
              className={`p-2 rounded border text-left flex flex-col justify-between ${
                profile === 'vedant' ? 'border-amber-400 bg-amber-500/20 text-amber-200' : 'border-line bg-bg text-muted'
              }`}
            >
              <span className="font-semibold flex items-center gap-1"><UserCheck size={12} /> Vedant (21)</span>
              <span className="text-[10px] text-muted mt-1">Aggressive · 3 Strats</span>
            </button>
            <button
              type="button"
              onClick={() => selectPreset('salaried')}
              className={`p-2 rounded border text-left flex flex-col justify-between ${
                profile === 'salaried' ? 'border-sky-400 bg-sky-500/20 text-sky-200' : 'border-line bg-bg text-muted'
              }`}
            >
              <span className="font-semibold flex items-center gap-1"><TrendingUp size={12} /> Salaried (30)</span>
              <span className="text-[10px] text-muted mt-1">Moderate · ₹1L/mo</span>
            </button>
            <button
              type="button"
              onClick={() => selectPreset('senior')}
              className={`p-2 rounded border text-left flex flex-col justify-between ${
                profile === 'senior' ? 'border-emerald-400 bg-emerald-500/20 text-emerald-200' : 'border-line bg-bg text-muted'
              }`}
            >
              <span className="font-semibold flex items-center gap-1"><ShieldAlert size={12} /> Retiree (60)</span>
              <span className="text-[10px] text-muted mt-1">Conservative Capital</span>
            </button>
          </div>
        </div>

        {user?.role !== 'ADMIN' ? (
          <div className="space-y-3 text-sm"><p>No trading account is configured yet. Ask an ADMIN to complete account setup.</p>
            <button className="btn" onClick={async () => { await logout(); nav('/login') }}>Logout</button></div>
        ) : (
          <form onSubmit={submit} className="space-y-3">
            <label className="block text-xs text-muted">Account name<input className="input mt-1 w-full" value={name} onChange={(e) => setName(e.target.value)} required /></label>
            <label className="block text-xs text-muted">Starting capital (INR)<input className="input mt-1 w-full num" type="number" min={100000} step={1000} value={capital} onChange={(e) => setCapital(e.target.value)} required /></label>
            <div className="text-xs text-muted">
              Auto-allocates initial strategy capital (₹8,00,000 across 3 strategies with ₹2,00,000 cash reserve) and applies AI-calculated Risk Firewall thresholds based on the selected profile.
            </div>
            <ErrorText error={err} />
            <button className="btn btn-primary w-full" disabled={busy}>Configure Account &amp; Apply AI Risk Limits</button>
          </form>
        )}
      </Panel>
    </div>
  )
}
