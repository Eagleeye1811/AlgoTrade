import { useEffect, useRef, useSyncExternalStore, useCallback, useState } from 'react'
import { wsClient, WsEvent } from '../services/ws'
import { api } from '../services/api'

export function useEvents(types: string[], fn: (e: WsEvent) => void) {
  const ref = useRef(fn)
  ref.current = fn
  const key = types.join('|')
  useEffect(() => wsClient.subscribe(key.split('|'), (e) => ref.current(e)), [key])
}

export function useWsStatus() {
  return useSyncExternalStore((cb) => wsClient.onStatus(cb), () => wsClient.status)
}

/** Fetch a REST snapshot, re-fetch (throttled) when any of the given events arrive. */
export function useApi<T = any>(path: string | null, refreshOn: string[] = [], throttleMs = 800) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const timer = useRef<number | null>(null)
  const last = useRef(0)
  const load = useCallback(async () => {
    if (!path) return
    try { const d = await api.get<T>(path); setData(d); setError(null) } catch (e: any) { setError(e.message) } finally { setLoading(false) }
  }, [path])
  useEffect(() => { setLoading(true); load() }, [load])
  useEvents(refreshOn, () => {
    if (timer.current) return
    const wait = Math.max(0, throttleMs - (Date.now() - last.current))
    timer.current = window.setTimeout(() => { timer.current = null; last.current = Date.now(); load() }, wait)
  })
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])
  return { data, error, loading, reload: load, setData }
}
