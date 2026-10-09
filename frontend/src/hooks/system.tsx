import { createContext, ReactNode, useContext, useEffect } from 'react'
import { useApi, useEvents } from './useEvents'

export interface Overview {
  trading: { status: string; reason: string; blocks: Record<string, string>; orders_allowed: boolean; safe_mode: boolean }
  market: any
  broker: any
  health: { overall: string; components: any[] }
  portfolio: any
  strategies: Record<string, any>
  kill_switch: any
  reconciliation: { status: string; last: any }
  account_configured: boolean
  execution_mode: string
}

interface SysCtx { overview: Overview | null; error: string | null; reload: () => void; killSwitch: any }
const Ctx = createContext<SysCtx | null>(null)

export function SystemProvider({ children }: { children: ReactNode }) {
  const { data, error, reload, setData } = useApi<Overview>('/overview',
    ['trading.status', 'market.status', 'broker.status', 'reconciliation.completed', 'kill_switch.update', 'strategy.status'], 1000)
  const ks = useApi('/kill-switch', ['kill_switch.update', 'trading.status'], 500)
  useEvents(['pnl.update'], (e) => setData((d) => (d ? { ...d, portfolio: e.payload.portfolio, strategies: e.payload.strategies } : d)))
  useEvents(['health.update'], (e) => setData((d) => (d ? { ...d, health: e.payload } : d)))
  useEvents(['trading.status'], (e) => setData((d) => (d ? { ...d, trading: e.payload } : d)))
  useEffect(() => { const t = setInterval(reload, 15000); return () => clearInterval(t) }, [reload])
  return <Ctx.Provider value={{ overview: data, error, reload, killSwitch: ks.data }}>{children}</Ctx.Provider>
}

export function useSystem() {
  const c = useContext(Ctx)
  if (!c) throw new Error('SystemProvider missing')
  return c
}
