import { getToken } from './api'

export interface WsEvent {
  event_id?: string
  event_type: string
  timestamp?: string
  correlation_id?: string | null
  source?: string
  strategy_id?: string | null
  order_id?: string | null
  symbol?: string | null
  payload: any
}
type Listener = (e: WsEvent) => void
export type WsStatus = 'connecting' | 'open' | 'closed'

class WsClient {
  private ws: WebSocket | null = null
  private listeners = new Map<string, Set<Listener>>()
  private statusListeners = new Set<() => void>()
  private seen = new Set<string>()
  private seenOrder: string[] = []
  private retry = 0
  private timer: number | null = null
  private symbols: string[] = []
  private stopped = true
  status: WsStatus = 'closed'

  connect() {
    this.stopped = false
    if (this.ws && this.ws.readyState <= 1) return
    const token = getToken()
    if (!token) return
    this.setStatus('connecting')
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`)
    this.ws = ws
    ws.onopen = () => { this.retry = 0; this.setStatus('open'); this.sendSymbols() }
    ws.onmessage = (m) => {
      let e: WsEvent
      try { e = JSON.parse(m.data) } catch { return }
      if (e.event_id) {
        if (this.seen.has(e.event_id)) return
        this.seen.add(e.event_id); this.seenOrder.push(e.event_id)
        if (this.seenOrder.length > 5000) this.seen.delete(this.seenOrder.shift()!)
      }
      this.dispatch(e)
    }
    ws.onclose = () => {
      this.ws = null
      this.setStatus('closed')
      if (this.stopped) return
      const delay = Math.min(15000, 500 * 2 ** this.retry++)
      this.timer = window.setTimeout(() => this.connect(), delay)
    }
    ws.onerror = () => ws.close()
  }

  disconnect() {
    this.stopped = true
    if (this.timer) clearTimeout(this.timer)
    this.ws?.close()
  }

  private dispatch(e: WsEvent) {
    for (const key of [e.event_type, '*']) this.listeners.get(key)?.forEach((l) => { try { l(e) } catch (err) { console.error(err) } })
  }

  private setStatus(s: WsStatus) { this.status = s; this.statusListeners.forEach((l) => l()) }

  subscribe(types: string[], fn: Listener): () => void {
    for (const t of types) {
      if (!this.listeners.has(t)) this.listeners.set(t, new Set())
      this.listeners.get(t)!.add(fn)
    }
    return () => types.forEach((t) => this.listeners.get(t)?.delete(fn))
  }

  onStatus(fn: () => void) { this.statusListeners.add(fn); return () => { this.statusListeners.delete(fn) } }

  setSymbols(symbols: string[]) { this.symbols = symbols; this.sendSymbols() }
  private sendSymbols() { if (this.ws?.readyState === 1) this.ws.send(JSON.stringify({ symbols: this.symbols })) }
}

export const wsClient = new WsClient()
