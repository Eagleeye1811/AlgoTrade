const TOKEN_KEY = 'algotrade.token'
const USER_KEY = 'algotrade.user'

export interface User { id: number; email: string; name: string; role: 'ADMIN' | 'TRADER' }

export function getToken(): string | null {
  try { return localStorage.getItem(TOKEN_KEY) } catch { return null }
}
export function getStoredUser(): User | null {
  try { const v = localStorage.getItem(USER_KEY); return v ? JSON.parse(v) : null } catch { return null }
}
export function setSession(token: string, user: User) {
  try { localStorage.setItem(TOKEN_KEY, token); localStorage.setItem(USER_KEY, JSON.stringify(user)) } catch { /* ignore */ }
}
export function clearSession() {
  try { localStorage.removeItem(TOKEN_KEY); localStorage.removeItem(USER_KEY) } catch { /* ignore */ }
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) { super(message); this.status = status }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  let res: Response
  try {
    res = await fetch(`/api${path}`, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined })
  } catch {
    throw new ApiError(0, 'Backend unreachable')
  }
  if (res.status === 401 && !path.startsWith('/auth/login')) {
    clearSession()
    if (!location.pathname.startsWith('/login') && !location.pathname.startsWith('/register')) location.href = '/login'
  }
  const text = await res.text()
  let data: any = null
  try { data = text ? JSON.parse(text) : null } catch { data = text }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    if (data?.detail) msg = typeof data.detail === 'string' ? data.detail : data.detail.map((d: any) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join('; ')
    throw new ApiError(res.status, msg)
  }
  return data as T
}

export const api = {
  get: <T = any>(p: string) => request<T>('GET', p),
  post: <T = any>(p: string, b?: unknown) => request<T>('POST', p, b ?? {}),
  put: <T = any>(p: string, b?: unknown) => request<T>('PUT', p, b ?? {}),
}
