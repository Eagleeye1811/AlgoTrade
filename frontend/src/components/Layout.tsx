import { useEffect, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { Activity, AlertOctagon, BookOpen, Bug, CandlestickChart, LayoutDashboard, ListOrdered, LogOut, Pause, Play, Settings, ShieldCheck, Wallet } from 'lucide-react'
import { useAuth } from '../hooks/auth'
import { useSystem } from '../hooks/system'
import { useWsStatus } from '../hooks/useEvents'
import { api } from '../services/api'
import { fmtQty } from '../services/format'
import { wsClient } from '../services/ws'
import { Badge, ErrorText, Modal, StatusBadge } from './ui'

const NAV = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/strategies', label: 'Strategies', icon: CandlestickChart, end: true },
  { to: '/orders', label: 'Orders, Trades & P&L', icon: ListOrdered },
  { to: '/positions', label: 'Positions', icon: Wallet },
  { to: '/risk', label: 'Risk', icon: ShieldCheck },
  { to: '/health', label: 'Reconciliation & Health', icon: Activity },
  { to: '/faults', label: 'Fault Simulation', icon: Bug },
  { to: '/audit', label: 'Audit Trail', icon: BookOpen },
  { to: '/settings', label: 'Settings / Account', icon: Settings },
]

// outside NSE hours the platform replays the recorded 021 session (or uses the sandbox stream): say so
function afterHoursNote(m: any) {
  const kinds = Object.values(m?.after_hours ?? {}) as string[]
  if (!kinds.length) return null
  const day = m.replay_day ? new Date(`${m.replay_day}T00:00:00+05:30`).toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short' }) : ''
  return kinds.includes('replay')
    ? <> · <span className="text-amber-300">after hours: replay of the 021 session of {day}</span></>
    : <> · <span className="text-amber-300">after hours: 021 sandbox stream</span></>
}

export default function Layout() {
  const { user, logout } = useAuth()
  const { overview } = useSystem()
  const nav = useNavigate()
  useEffect(() => { wsClient.connect() }, [])
  useEffect(() => { if (overview && !overview.account_configured) nav('/setup') }, [overview, nav])
  return (
    <div className="flex h-screen flex-col overflow-hidden">
      <TopBar />
      <SafeModeBanner />
      <div className="flex min-h-0 flex-1">
        <aside className="hidden w-52 shrink-0 flex-col border-r border-line bg-panel md:flex">
          <nav className="flex-1 overflow-auto py-2">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to} end={n.end}
                className={({ isActive }) => `flex items-center gap-2 px-4 py-1.5 ${isActive ? 'bg-panel2 text-white border-l-2 border-info' : 'text-muted hover:text-text border-l-2 border-transparent'}`}>
                <n.icon size={14} />{n.label}
              </NavLink>
            ))}
          </nav>
          <div className="border-t border-line p-3 text-xs">
            <div className="font-medium">{user?.name}</div>
            <div className="text-muted truncate">{user?.email}</div>
            <div className="mt-1 flex items-center justify-between"><Badge tone={user?.role === 'ADMIN' ? 'purple' : 'blue'}>{user?.role}</Badge>
              <button className="btn" onClick={async () => { await logout(); nav('/login') }}><LogOut size={12} />Logout</button></div>
          </div>
        </aside>
        <main className="relative min-w-0 flex-1 overflow-auto">
          <MobileNav />
          <div className="p-3 md:p-4"><Outlet /></div>
        </main>
      </div>
    </div>
  )
}

function MobileNav() {
  return (
    <div className="flex gap-1 overflow-x-auto border-b border-line px-2 py-1.5 md:hidden">
      {NAV.map((n) => (
        <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => `whitespace-nowrap rounded px-2 py-1 text-xs ${isActive ? 'bg-panel2 text-white' : 'text-muted'}`}>{n.label}</NavLink>
      ))}
    </div>
  )
}

function TopBar() {
  const { overview, reload } = useSystem()
  const ws = useWsStatus()
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [killOpen, setKillOpen] = useState(false)
  const t = overview?.trading
  const m = overview?.market
  const act = async (path: string) => {
    setBusy(true); setErr(null)
    try { await api.post(path); reload() } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <header className="border-b border-line bg-panel">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-3 py-2">
        <div className="flex items-center gap-2 pr-2">
          <img src="/logo.png" alt="AlgoTrade" className="h-6 w-6 rounded-full object-contain" />
          <span className="text-[15px] font-bold tracking-tight text-white">AlgoTrade</span>
        </div>
        {overview?.execution_mode && <Badge tone="purple">{overview.broker?.paper ? 'paper trading · no real orders' : '021 sandbox · simulated exchange'}</Badge>}
        <div className="flex items-center gap-1.5 text-xs" title={m?.source}>
          <span className="text-muted">Market</span><StatusBadge status={m?.display_status} />
        </div>
        <div className="flex items-center gap-1.5 text-xs"><span className="text-muted">Broker</span>
          <StatusBadge status={overview?.broker ? (overview.broker.available ? 'CONNECTED' : 'UNAVAILABLE') : undefined} />
          {overview?.broker?.current_latency_ms != null && <span className="num text-muted">{Math.round(overview.broker.current_latency_ms)}ms</span>}
        </div>
        <div className="flex items-center gap-1.5 text-xs"><span className="text-muted">Health</span><StatusBadge status={overview?.health?.overall} /></div>
        <div className="flex items-center gap-1.5 text-xs"><span className="text-muted">Trading</span><StatusBadge status={t?.status} /></div>
        <span title={`WebSocket ${ws}`} className={`h-2 w-2 rounded-full ${ws === 'open' ? 'bg-green-500' : ws === 'connecting' ? 'bg-amber-500 animate-pulse' : 'bg-red-500'}`} />
        <div className="ml-auto flex items-center gap-2">
          {t?.status === 'RUNNING'
            ? <button className="btn" disabled={busy} onClick={() => act('/trading/pause')}><Pause size={12} />Pause Trading</button>
            : <button className="btn btn-green" disabled={busy || t?.status === 'SAFE_MODE'} onClick={() => act('/trading/start')}><Play size={12} />Start Trading</button>}
          <button className="btn btn-danger font-bold" onClick={() => setKillOpen(true)}><AlertOctagon size={14} />KILL SWITCH</button>
        </div>
      </div>
      {(m?.source || (t && Object.keys(t.blocks).length > 0) || err) && (
        <div className="flex flex-wrap items-center gap-2 border-t border-line px-3 py-1 text-[11px]">
          {m?.source && <span className="text-muted">Data: {m.source}
            {afterHoursNote(m)}</span>}
          {t && Object.entries(t.blocks).map(([k, v]) => <Badge key={k} tone="amber" className="normal-case">Orders blocked: {v}</Badge>)}
          {err && <span className="text-red-400">{err}</span>}
        </div>
      )}
      <KillModal open={killOpen} onClose={() => setKillOpen(false)} />
    </header>
  )
}

function KillModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { reload } = useSystem()
  const [reason, setReason] = useState('manual activation from dashboard')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const go = async () => {
    setBusy(true); setErr(null)
    try { await api.post('/kill-switch/activate', { reason }); reload(); onClose() } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <Modal open={open} onClose={onClose} title="Emergency kill switch"
      footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn btn-danger" disabled={busy} onClick={go}><AlertOctagon size={13} />Activate kill switch</button></>}>
      <p className="text-sm">This immediately:</p>
      <ol className="list-decimal space-y-0.5 pl-5 text-xs text-muted">
        <li>stops all new strategy orders (SAFE MODE)</li><li>blocks new orders in the Risk Firewall</li>
        <li>cancels all pending orders</li><li>sends market orders to close every open position</li>
      </ol>
      <label className="block text-xs text-muted">Reason<input className="input mt-1 w-full" value={reason} onChange={(e) => setReason(e.target.value)} /></label>
      <ErrorText error={err} />
    </Modal>
  )
}

function SafeModeBanner() {
  const { overview, killSwitch, reload } = useSystem()
  const { user } = useAuth()
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  if (overview?.trading?.status !== 'SAFE_MODE') return null
  const ks = killSwitch || {}
  const act = async (p: string) => { setBusy(true); setErr(null); try { await api.post(p); reload() } catch (e: any) { setErr(e.message) } finally { setBusy(false) } }
  return (
    <div className="border-b border-red-700 bg-red-950/70 px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-sm font-bold text-red-300">🔴 EMERGENCY MODE ACTIVE</span>
        <span className="text-red-200">{overview.trading.reason}</span>
        {ks.phase && <Badge tone="red">{String(ks.phase).replace(/_/g, ' ')}</Badge>}
        {ks.cancelled && <span className="text-red-200">{ks.cancelled.length} order(s) cancelled · {ks.close_orders?.length ?? 0} close order(s)</span>}
        {ks.elapsed_s != null && <Badge tone={ks.within_deadline ? 'green' : 'amber'}>
          {ks.within_deadline ? `flat in ${Number(ks.elapsed_s).toFixed(2)}s (≤ ${ks.deadline_s ?? 10}s)` : `${Number(ks.elapsed_s).toFixed(2)}s - not fully flat`}</Badge>}
        {user?.role === 'ADMIN' && (
          <span className="ml-auto flex gap-2">
            {ks.unresolved?.length > 0 && <button className="btn" disabled={busy} onClick={() => act('/kill-switch/retry-close')}>Retry close</button>}
            <button className="btn" disabled={busy} onClick={() => act('/kill-switch/reset')}>Reset SAFE MODE</button>
          </span>
        )}
      </div>
      {ks.close_orders?.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-2 text-red-200">
          {ks.close_orders.map((c: any, i: number) => <span key={i} className="num">{c.strategy_id} {c.side} {fmtQty(c.quantity)} {c.symbol}: {c.status}{c.reason ? ` (${c.reason})` : ''}</span>)}
        </div>
      )}
      {ks.unresolved?.length > 0 && (
        <div className="mt-1 space-y-0.5">
          {ks.unresolved.map((u: any, i: number) => <div key={i} className="font-semibold text-red-300">UNRESOLVED POSITION: {u.strategy_id} {u.symbol} {fmtQty(u.quantity)} — {u.reason}</div>)}
        </div>
      )}
      {err && <div className="mt-1 text-red-300">{err}</div>}
    </div>
  )
}
