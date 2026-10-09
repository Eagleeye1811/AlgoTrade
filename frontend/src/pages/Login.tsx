import { FormEvent, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { BarChart3 } from 'lucide-react'
import { useAuth } from '../hooks/auth'
import { ErrorText } from '../components/ui'

export default function Login({ mode }: { mode: 'login' | 'register' }) {
  const { login, register } = useAuth()
  const nav = useNavigate()
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setErr(null)
    try {
      if (mode === 'login') await login(email, password)
      else await register(email, name, password)
      nav('/')
    } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <form onSubmit={submit} className="panel w-full max-w-sm space-y-3 p-6">
        <div className="flex items-center gap-2"><BarChart3 className="text-info" size={22} /><span className="text-xl font-bold text-white">AlgoTrade</span></div>
        <p className="text-xs text-muted">Fault-tolerant algorithmic trading &amp; risk control · 021 developer sandbox (virtual funds, simulated exchange)</p>
        <h1 className="pt-2 text-sm font-semibold">{mode === 'login' ? 'Sign in' : 'Create account'}</h1>
        {mode === 'register' && <input className="input w-full" placeholder="Full name" value={name} onChange={(e) => setName(e.target.value)} required />}
        <input className="input w-full" type="email" placeholder="Email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email" />
        <input className="input w-full" type="password" placeholder="Password (min 8 chars)" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={mode === 'register' ? 8 : 1} autoComplete={mode === 'login' ? 'current-password' : 'new-password'} />
        <ErrorText error={err} />
        <button className="btn btn-primary w-full justify-center" disabled={busy}>{busy ? 'Please wait…' : mode === 'login' ? 'Sign in' : 'Register'}</button>
        <div className="text-center text-xs text-muted">
          {mode === 'login' ? <>No account? <Link className="text-info" to="/register">Register</Link></> : <>Have an account? <Link className="text-info" to="/login">Sign in</Link></>}
        </div>
        {mode === 'register' && <p className="text-[11px] text-muted">The first registered user becomes ADMIN; later users are TRADERs.</p>}
      </form>
    </div>
  )
}
