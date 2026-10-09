import { useEffect, useMemo, useRef, useState } from 'react'
import { Search, Star } from 'lucide-react'
import { useApi, useEvents } from '../hooks/useEvents'
import { fmtNum, fmtQty, toNum } from '../services/format'

interface Row {
  symbol: string; ltp: number | null; position: number; fresh: boolean
  open?: number; high?: number; low?: number; volume?: number
  ref_price?: number; ref_basis?: 'prev_close' | 'open'; change?: number; change_pct?: number | null; spark?: number[]
}
type SortKey = 'default' | 'gainers' | 'losers' | 'az'

const FAV_KEY = 'algotrade.watchlist.favs'
const SORT_KEY = 'algotrade.watchlist.sort'
const load = <T,>(k: string, d: T): T => { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d } catch { return d } }
const save = (k: string, v: unknown) => { try { localStorage.setItem(k, JSON.stringify(v)) } catch { /* storage unavailable */ } }

const compact = (v: unknown) => {
  const n = toNum(v)
  if (n === null) return '—'
  if (n >= 1e7) return `${(n / 1e7).toFixed(2)}Cr`
  if (n >= 1e5) return `${(n / 1e5).toFixed(2)}L`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`
  return fmtQty(n)
}

function Spark({ pts, up }: { pts?: number[]; up: boolean }) {
  if (!pts || pts.length < 2) return <svg width="64" height="24" />
  const lo = Math.min(...pts), hi = Math.max(...pts), span = hi - lo || 1
  const d = pts.map((p, i) => `${((i / (pts.length - 1)) * 62 + 1).toFixed(1)},${(22 - ((p - lo) / span) * 20).toFixed(1)}`).join(' ')
  const color = up ? '#22c55e' : '#ef4444'
  return (
    <svg width="64" height="24" className="shrink-0">
      <polyline points={`1,23 ${d} 63,23`} fill={up ? 'rgba(34,197,94,.10)' : 'rgba(239,68,68,.10)'} stroke="none" />
      <polyline points={d} fill="none" stroke={color} strokeWidth="1.3" strokeLinejoin="round" />
    </svg>
  )
}

/** Live watchlist of every tracked NSE symbol. Clicking a row selects it (e.g. in the market chart). */
export default function Watchlist({ selected, onSelect }: { selected?: string; onSelect?: (s: string) => void }) {
  // ticks update prices live; the snapshot (volume, sparkline history, positions) refreshes on fills and every 15s
  const wl = useApi<Row[]>('/market/watchlist', ['position.update'], 1000)
  const [q, setQ] = useState('')
  const [favs, setFavs] = useState<string[]>(() => load(FAV_KEY, []))
  const [sort, setSort] = useState<SortKey>(() => load(SORT_KEY, 'default'))
  const [ticks, setTicks] = useState<Record<string, number>>({})
  const [flash, setFlash] = useState<Record<string, 'up' | 'down'>>({})
  const pending = useRef<Record<string, number>>({})
  const frame = useRef<number | null>(null)
  const last = useRef<Record<string, number>>({})
  const flashTimers = useRef<Record<string, number>>({})

  useEffect(() => { const t = window.setInterval(wl.reload, 15000); return () => clearInterval(t) }, [wl.reload])
  useEffect(() => () => {
    if (frame.current) cancelAnimationFrame(frame.current)
    Object.values(flashTimers.current).forEach(clearTimeout)
  }, [])

  // batch ticks into one render per animation frame (up to ~60 ticks/s across 15 symbols)
  useEvents(['market.tick'], (e) => {
    const p = toNum(e.payload?.price)
    if (!e.symbol || p === null) return
    pending.current[e.symbol] = p
    if (frame.current) return
    frame.current = requestAnimationFrame(() => {
      frame.current = null
      const batch = pending.current
      pending.current = {}
      const dirs: Record<string, 'up' | 'down'> = {}
      for (const [sym, price] of Object.entries(batch)) {
        const was = last.current[sym]
        if (was !== undefined && price !== was) {
          dirs[sym] = price > was ? 'up' : 'down'
          clearTimeout(flashTimers.current[sym])
          flashTimers.current[sym] = window.setTimeout(() => setFlash((f) => { const n = { ...f }; delete n[sym]; return n }), 600)
        }
        last.current[sym] = price
      }
      setTicks((t) => ({ ...t, ...batch }))
      if (Object.keys(dirs).length) setFlash((f) => ({ ...f, ...dirs }))
    })
  })

  // overlay the latest tick on the snapshot: price, change, day range and the sparkline's last point
  const live = useMemo<Row[]>(() => (wl.data || []).map((r) => {
    const t = ticks[r.symbol]
    if (t === undefined) return r
    const ref = toNum(r.ref_price), hi = toNum(r.high), lo = toNum(r.low)
    return {
      ...r, ltp: t,
      change: ref !== null ? t - ref : r.change,
      change_pct: ref ? ((t - ref) / ref) * 100 : r.change_pct,
      high: hi !== null ? Math.max(hi, t) : r.high,
      low: lo !== null ? Math.min(lo, t) : r.low,
      spark: r.spark && r.spark.length ? [...r.spark.slice(0, -1), t] : r.spark,
    }
  }), [wl.data, ticks])

  const toggleFav = (s: string) => setFavs((f) => { const n = f.includes(s) ? f.filter((x) => x !== s) : [...f, s]; save(FAV_KEY, n); return n })
  const pickSort = (s: SortKey) => { setSort(s); save(SORT_KEY, s) }

  const rows = useMemo(() => {
    const list = live.filter((r) => r.symbol.includes(q.trim().toUpperCase()))
    const pct = (r: Row) => toNum(r.change_pct) ?? 0
    const order = [...list]
    if (sort === 'gainers') order.sort((a, b) => pct(b) - pct(a))
    else if (sort === 'losers') order.sort((a, b) => pct(a) - pct(b))
    else if (sort === 'az') order.sort((a, b) => a.symbol.localeCompare(b.symbol))
    return order.sort((a, b) => Number(favs.includes(b.symbol)) - Number(favs.includes(a.symbol)))
  }, [live, q, sort, favs])

  const adv = live.filter((r) => (toNum(r.change) ?? 0) > 0).length
  const dec = live.filter((r) => (toNum(r.change) ?? 0) < 0).length

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="space-y-2 border-b border-line px-3 py-2.5">
        <div className="flex items-center justify-between">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-muted">Watchlist</span>
          <span className="text-[11px] text-muted"><span className="text-pos">{adv} ▲</span> · <span className="text-neg">{dec} ▼</span></span>
        </div>
        <label className="flex items-center gap-1.5 rounded-md border border-line bg-bg px-2 focus-within:border-info">
          <Search size={13} className="text-muted" />
          <input className="w-full bg-transparent py-1.5 text-xs outline-none placeholder:text-muted" placeholder="Search symbol"
            value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
        <div className="flex gap-1">
          {([['default', 'Default'], ['gainers', 'Gainers'], ['losers', 'Losers'], ['az', 'A–Z']] as [SortKey, string][]).map(([k, l]) => (
            <button key={k} onClick={() => pickSort(k)}
              className={`rounded px-2 py-0.5 text-[11px] font-medium transition-colors ${sort === k ? 'bg-info/15 text-info' : 'text-muted hover:text-text'}`}>{l}</button>
          ))}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {!wl.data ? <div className="p-4 text-xs text-muted">{wl.error ?? 'Loading…'}</div>
          : rows.length === 0 ? <div className="p-4 text-center text-xs text-muted">No symbol matches “{q}”</div>
          : rows.map((r) => {
            const ch = toNum(r.change), up = (ch ?? 0) >= 0, fav = favs.includes(r.symbol), sel = r.symbol === selected
            const pos = toNum(r.position) ?? 0
            const fl = flash[r.symbol]
            return (
              <div key={r.symbol} role="button" tabIndex={0} onClick={() => onSelect?.(r.symbol)}
                onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && onSelect?.(r.symbol)}
                className={`group flex cursor-pointer items-center gap-2 border-b border-line/60 py-2 pl-1.5 pr-3 transition-colors ${sel ? 'bg-info/10 shadow-[inset_2px_0_0_#3b82f6]' : 'hover:bg-panel2'}`}>
                <button onClick={(e) => { e.stopPropagation(); toggleFav(r.symbol) }} title={fav ? 'Unpin' : 'Pin to top'}
                  className={`p-0.5 ${fav ? 'text-amber-400' : 'text-transparent group-hover:text-muted'}`}>
                  <Star size={12} fill={fav ? 'currentColor' : 'none'} />
                </button>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className={`truncate text-[13px] font-semibold ${sel ? 'text-white' : 'text-text'}`}>{r.symbol}</span>
                    {pos !== 0 && <span className={`rounded px-1 text-[10px] font-semibold num ${pos > 0 ? 'bg-pos/15 text-pos' : 'bg-neg/15 text-neg'}`}
                      title="Net position across strategies">{pos > 0 ? '+' : ''}{fmtQty(pos)}</span>}
                    {!r.fresh && <span className="h-1.5 w-1.5 rounded-full bg-amber-400" title="No fresh tick" />}
                  </div>
                  <div className="truncate text-[10px] text-muted num">
                    {r.high != null ? <>H {fmtNum(r.high)} · L {fmtNum(r.low)} · {compact(r.volume)}</> : 'no candles yet'}
                  </div>
                </div>
                <Spark pts={r.spark} up={up} />
                <div className="w-[76px] text-right">
                  <div className={`num rounded px-1 text-[13px] font-semibold transition-colors duration-500 ${fl === 'up' ? 'bg-pos/20' : fl === 'down' ? 'bg-neg/20' : ''} text-white`}>
                    {fmtNum(r.ltp)}
                  </div>
                  <div className={`num text-[11px] ${ch === null ? 'text-muted' : up ? 'text-pos' : 'text-neg'}`}
                    title={r.ref_basis === 'prev_close' ? `vs previous close ${fmtNum(r.ref_price)}` : `vs today's open ${fmtNum(r.ref_price)}`}>
                    {r.change_pct == null ? '—' : `${up ? '+' : ''}${fmtNum(r.change_pct, 2)}%`}
                  </div>
                </div>
              </div>
            )
          })}
      </div>
    </div>
  )
}
