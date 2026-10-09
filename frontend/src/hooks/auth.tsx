import { createContext, ReactNode, useCallback, useContext, useState } from 'react'
import { api, clearSession, getStoredUser, getToken, setSession, User } from '../services/api'
import { wsClient } from '../services/ws'

interface AuthCtx {
  user: User | null
  login: (email: string, password: string) => Promise<void>
  register: (email: string, name: string, password: string) => Promise<void>
  logout: () => Promise<void>
}
const Ctx = createContext<AuthCtx | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(() => (getToken() ? getStoredUser() : null))
  const accept = (d: any) => { setSession(d.access_token, d.user); setUser(d.user) }
  const login = useCallback(async (email: string, password: string) => accept(await api.post('/auth/login', { email, password })), [])
  const register = useCallback(async (email: string, name: string, password: string) => accept(await api.post('/auth/register', { email, name, password })), [])
  const logout = useCallback(async () => {
    try { await api.post('/auth/logout') } catch { /* session may already be invalid */ }
    wsClient.disconnect()
    clearSession()
    setUser(null)
  }, [])
  return <Ctx.Provider value={{ user, login, register, logout }}>{children}</Ctx.Provider>
}

export function useAuth() {
  const c = useContext(Ctx)
  if (!c) throw new Error('AuthProvider missing')
  return c
}
