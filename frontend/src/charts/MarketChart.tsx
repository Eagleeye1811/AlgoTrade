import { useEffect, useRef, useState } from 'react'
import { CandlestickData, HistogramData, IChartApi, ISeriesApi, LineStyle, Time } from 'lightweight-charts'
import { Maximize2, Minimize2, ScanLine } from 'lucide-react'
import { api } from '../services/api'
import { wsClient } from '../services/ws'
import { useEvents } from '../hooks/useEvents'
import { fmtNum, fmtQty } from '../services/format'
import { StatusBadge } from '../components/ui'
import { useSystem } from '../hooks/system'
import { useAuth } from '../hooks/auth'
import { IST_OFFSET, makeChart, signalMarkers, toChartTime } from './base'

interface Candle { time: number; open: number; high: number; low: number; close: number; volume: number }
interface Legend { time: number; open: number; high: number; low: number; close: number; volume: number | null }

const UP = '#22c55e'
const DOWN = '#ef4444'
const volColor = (c: { open: number; close: number }) => (c.close >= c.open ? 'rgba(34,197,94,.28)' : 'rgba(239,68,68,.28)')
// IST calendar day of a UTC epoch-seconds timestamp
const istDay = (t: number) => Math.floor((t + IST_OFFSET) / 86400)
const TF_SECONDS = { '1m': 60, '5m': 300 } as const
const fmtCountdown = (s: number) => {
  const t = Math.max(0, Math.ceil(s))
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60
  return (h ? `${h}:${String(m).padStart(2, '0')}` : String(m).padStart(2, '0')) + `:${String(sec).padStart(2, '0')}`
}
const fmtLegendTime = (t: number) =>
  new Date(t * 1000).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false, day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })

export default function MarketChart({ symbols, symbol: controlled, onSymbolChange }:
  { symbols: string[]; symbol?: string; onSymbolChange?: (s: string) => void }) {
  const [inner, setInner] = useState<string>('')
  const symbol = controlled ?? inner
  const setSymbol = (s: string) => { setInner(s); onSymbolChange?.(s) }
  const [tf, setTf] = useState<'1m' | '5m'>('1m')
  const [ltp, setLtp] = useState<number | null>(null)
  const [sessionOpen, setSessionOpen] = useState<number | null>(null)
  const [legend, setLegend] = useState<Legend | null>(null)
  const [status, setStatus] = useState<string | null>(null)
  const [count, setCount] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState(false)
  const el = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const cs = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const vs = useRef<ISeriesApi<'Histogram'> | null>(null)
  const lastTime = useRef<number>(0)
  const firstTime = useRef<number | null>(null)
  const lastCandle = useRef<Candle | null>(null)
  const hovering = useRef(false)
  // last-price tag on the price scale: price + time left in the forming candle (like TradingView)
  const tag = useRef<HTMLDivElement>(null)
  const tagPrice = useRef<HTMLDivElement>(null)
  const tagTime = useRef<HTMLDivElement>(null)
  // market clock: LIVE follows the wall clock; REPLAY is extrapolated from the latest tick's market time × speed
  const clock = useRef<{ mode?: string; speed: number; status: string | null; tf: '1m' | '5m'; tickMarket: number | null; tickWall: number }>(
    { speed: 1, status: null, tf: '1m', tickMarket: null, tickWall: 0 })
  const { overview, reload } = useSystem()
  const { user } = useAuth()
  const market = overview?.market
  const isReplay = market?.mode === 'REPLAY'
  clock.current.mode = market?.mode
  clock.current.speed = isReplay ? Number(market?.speed) || 1 : 1
  clock.current.status = status
  clock.current.tf = tf
  const setSpeed = async (speed: number) => {
    try { await api.post('/market/replay', { speed }); reload() } catch (e: any) { setError(e.message) }
  }

  useEffect(() => { if (!symbol && symbols.length) setSymbol(symbols[0]) }, [symbols, symbol])

  useEffect(() => {
    if (!expanded) return
    const h = (e: KeyboardEvent) => e.key === 'Escape' && setExpanded(false)
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [expanded])

  useEffect(() => {
    const node = el.current
    if (!node) return
    const c = makeChart(node, node.clientHeight || 520)
    c.applyOptions({
      layout: { fontSize: 12 },
      grid: { vertLines: { color: 'rgba(31,42,55,.45)' }, horzLines: { color: 'rgba(31,42,55,.45)' } },
      rightPriceScale: { scaleMargins: { top: 0.08, bottom: 0.22 } },
      timeScale: { rightOffset: 6, barSpacing: 9, minBarSpacing: 2 },
      crosshair: {
        vertLine: { color: '#475569', style: LineStyle.Dashed, labelBackgroundColor: '#1e293b' },
        horzLine: { color: '#475569', style: LineStyle.Dashed, labelBackgroundColor: '#1e293b' },
      },
    })
    cs.current = c.addCandlestickSeries({ upColor: UP, downColor: DOWN, borderVisible: false, wickUpColor: UP, wickDownColor: DOWN,
      lastValueVisible: false })  // replaced by the price + countdown tag below
    vs.current = c.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false })
    c.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } })
    c.subscribeCrosshairMove((p) => {
      const bar = p.time !== undefined && cs.current ? (p.seriesData.get(cs.current) as CandlestickData | undefined) : undefined
      if (!bar) { hovering.current = false; showLast(lastCandle.current); return }
      const v = vs.current ? (p.seriesData.get(vs.current) as HistogramData | undefined) : undefined
      hovering.current = true
      setLegend({ time: (p.time as number) - IST_OFFSET, open: bar.open, high: bar.high, low: bar.low, close: bar.close, volume: v?.value ?? null })
    })
    chart.current = c
    // fill the container in both directions (height follows the CSS-sized wrapper)
    const ro = new ResizeObserver(() => c.applyOptions({ width: node.clientWidth, height: node.clientHeight }))
    ro.observe(node)
    const timer = window.setInterval(drawTag, 200)
    return () => { clearInterval(timer); ro.disconnect(); c.remove(); chart.current = null }
  }, [])

  // positioned imperatively (5×/s) so the countdown ticks without re-rendering the chart component
  function drawTag() {
    const el = tag.current, c = chart.current, series = cs.current, last = lastCandle.current
    if (!el || !c || !series || !last) { if (el) el.style.display = 'none'; return }
    const y = series.priceToCoordinate(last.close)
    const paneH = (c.timeScale().height() ? (el.parentElement?.clientHeight ?? 0) - c.timeScale().height() : 0)
    if (y === null || y < 0 || y > paneH) { el.style.display = 'none'; return }
    const k = clock.current
    let left: number | null = null
    if (k.status === 'LIVE') {
      left = last.time + TF_SECONDS[k.tf] - Date.now() / 1000
    } else if (k.status === 'REPLAY' && k.tickMarket !== null) {
      const marketNow = k.tickMarket + ((Date.now() - k.tickWall) / 1000) * k.speed
      left = (last.time + TF_SECONDS[k.tf] - marketNow) / k.speed
    }
    const up = last.close >= last.open
    el.style.display = 'block'
    el.style.width = `${c.priceScale('right').width()}px`
    el.style.background = up ? UP : DOWN
    tagPrice.current!.textContent = last.close.toFixed(2)
    tagTime.current!.textContent = left === null ? '' : fmtCountdown(Math.min(left, TF_SECONDS[k.tf]))
    tagTime.current!.style.display = left === null ? 'none' : 'block'
    el.style.top = `${y - el.offsetHeight / 2}px`
  }

  useEffect(() => { chart.current?.applyOptions({ watermark: { visible: !!symbol, text: symbol ? `${symbol} · ${tf}` : '', color: 'rgba(125,138,156,.07)', fontSize: 64 } }) }, [symbol, tf])

  const showLast = (c: Candle | null) => { if (c) setLegend({ ...c }) }

  const load = async (sym: string, timeframe: string, keepView = false) => {
    try {
      const d = await api.get(`/market/candles/${sym}?timeframe=${timeframe}&limit=400`)
      const candles: Candle[] = d.candles
      setError(null); setStatus(d.status); setCount(candles.length)
      cs.current?.setData(candles.map((c) => ({ time: toChartTime(c.time) as Time, open: c.open, high: c.high, low: c.low, close: c.close })))
      vs.current?.setData(candles.map((c) => ({ time: toChartTime(c.time) as Time, value: c.volume, color: volColor(c) })))
      const last = candles.length ? candles[candles.length - 1] : null
      lastTime.current = last ? last.time : 0
      firstTime.current = candles.length ? candles[0].time : null
      lastCandle.current = last
      if (last) {
        setLtp(last.close)
        const day = istDay(last.time)
        setSessionOpen(candles.find((c) => istDay(c.time) === day)?.open ?? null)
      } else { setLtp(null); setSessionOpen(null) }
      showLast(last)
      cs.current?.setMarkers(signalMarkers(d.signals, firstTime.current, true))
      if (!keepView) chart.current?.timeScale().scrollToRealTime()
    } catch (e: any) { setError(e.message) }
  }

  useEffect(() => {
    if (!symbol) return
    wsClient.setSymbols([symbol])
    load(symbol, tf)
    return () => wsClient.setSymbols([])
  }, [symbol, tf])

  useEvents(['candle.update'], (e) => {
    if (e.symbol !== symbol || e.payload.timeframe !== tf) return
    const c: Candle = e.payload.candle
    if (c.time < lastTime.current) return
    if (lastCandle.current && istDay(c.time) !== istDay(lastCandle.current.time)) setSessionOpen(c.open)
    lastTime.current = c.time
    lastCandle.current = c
    cs.current?.update({ time: toChartTime(c.time) as Time, open: c.open, high: c.high, low: c.low, close: c.close })
    vs.current?.update({ time: toChartTime(c.time) as Time, value: c.volume, color: volColor(c) })
    setLtp(c.close)
    if (!hovering.current) showLast(c)
  })
  useEvents(['market.tick'], (e) => {
    if (e.payload?.market_ts) { clock.current.tickMarket = Date.parse(e.payload.market_ts) / 1000; clock.current.tickWall = Date.now() }
    if (e.symbol === symbol) setLtp(e.payload.price)
  })
  useEvents(['market.status'], (e) => setStatus(e.payload.display_status))
  const refreshMarkers = useRef<number | null>(null)
  useEvents(['strategy.signal'], (e) => {
    if (e.symbol !== symbol || refreshMarkers.current) return
    refreshMarkers.current = window.setTimeout(() => { refreshMarkers.current = null; load(symbol, tf, true) }, 600)
  })

  const change = ltp !== null && sessionOpen ? ltp - sessionOpen : null
  const changePct = change !== null && sessionOpen ? (change / sessionOpen) * 100 : null
  const tone = change === null || change === 0 ? 'text-text' : change > 0 ? 'text-pos' : 'text-neg'
  const legendTone = legend && legend.close >= legend.open ? 'text-pos' : 'text-neg'

  return (
    <div className={expanded ? 'fixed inset-0 z-50 flex flex-col bg-panel' : 'flex flex-col'}>
      {/* header: instrument, price, controls */}
      <div className="flex flex-wrap items-end gap-x-6 gap-y-2 px-4 pt-3 pb-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-xl font-semibold tracking-tight text-white">{symbol || '—'}</span>
            <span className="rounded border border-line px-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted">NSE</span>
            <StatusBadge status={status} />
          </div>
          <div className="mt-0.5 flex items-baseline gap-3">
            <span className="num text-[28px] font-semibold leading-none text-white">{ltp !== null ? fmtNum(ltp) : '—'}</span>
            {change !== null && (
              <span className={`num text-sm font-medium ${tone}`}>
                {change > 0 ? '+' : ''}{fmtNum(change)} ({changePct! > 0 ? '+' : ''}{fmtNum(changePct, 2)}%)
                <span className="ml-1.5 text-[11px] font-normal text-muted">vs session open</span>
              </span>
            )}
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <div className="flex overflow-hidden rounded-md border border-line bg-bg p-0.5">
            {(['1m', '5m'] as const).map((x) => (
              <button key={x} onClick={() => setTf(x)}
                className={`rounded px-3 py-1 text-xs font-medium transition-colors ${tf === x ? 'bg-info text-white' : 'text-muted hover:text-text'}`}>{x}</button>
            ))}
          </div>
          <button className="btn px-2" title="Fit all candles" onClick={() => chart.current?.timeScale().fitContent()}><ScanLine size={14} /></button>
          <button className="btn px-2" title={expanded ? 'Exit full screen (Esc)' : 'Full screen'} onClick={() => setExpanded((x) => !x)}>
            {expanded ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          </button>
        </div>
      </div>

      {/* symbol strip */}
      <div className="flex gap-1 overflow-x-auto border-b border-line px-4 pb-2">
        {symbols.map((s) => (
          <button key={s} onClick={() => setSymbol(s)}
            className={`shrink-0 rounded-md px-2.5 py-1 text-[11px] font-semibold tracking-wide transition-colors ${s === symbol ? 'bg-info/15 text-info ring-1 ring-info/40' : 'text-muted hover:bg-panel2 hover:text-text'}`}>
            {s}
          </button>
        ))}
      </div>

      {error && <div className="px-4 py-1 text-xs text-red-400">{error}</div>}

      {/* chart */}
      <div className={`relative ${expanded ? 'min-h-0 flex-1' : 'h-[clamp(460px,66vh,820px)]'}`}>
        <div ref={el} className="absolute inset-0" />
        <div ref={tag} className="pointer-events-none absolute right-0 z-10 hidden rounded-l-sm px-1.5 py-0.5 text-center text-white shadow">
          <div ref={tagPrice} className="num text-[12px] font-semibold leading-tight" />
          <div ref={tagTime} className="num text-[11px] leading-tight opacity-90" />
        </div>
        {legend && (
          <div className="pointer-events-none absolute left-3 top-2 z-10 flex flex-wrap items-center gap-x-3 gap-y-0.5 rounded bg-panel/80 px-2 py-1 text-[11px] backdrop-blur-sm">
            <span className="text-muted">{fmtLegendTime(legend.time)}</span>
            <span className="num"><span className="text-muted">O </span><span className={legendTone}>{fmtNum(legend.open)}</span></span>
            <span className="num"><span className="text-muted">H </span><span className={legendTone}>{fmtNum(legend.high)}</span></span>
            <span className="num"><span className="text-muted">L </span><span className={legendTone}>{fmtNum(legend.low)}</span></span>
            <span className="num"><span className="text-muted">C </span><span className={legendTone}>{fmtNum(legend.close)}</span></span>
            {legend.volume !== null && <span className="num"><span className="text-muted">Vol </span>{fmtQty(legend.volume)}</span>}
          </div>
        )}
      </div>

      {/* footer: feed details */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-line px-4 py-1.5 text-[11px] text-muted">
        {isReplay && (
          <span className="flex items-center gap-1">
            {user?.role === 'ADMIN' ? (
              <select className="input py-0.5 text-[11px]" value={market.speed} onChange={(e) => setSpeed(Number(e.target.value))}
                title="Replay speed (system-wide)">
                {[1, 5, 10, 30, 60].map((x) => <option key={x} value={x}>{x === 1 ? '1x real-time' : `${x}x`}</option>)}
              </select>
            ) : <span className="num">{market.speed === 1 ? '1x real-time' : `${market.speed}x`}</span>}
            <span title="Only 1-minute OHLCV is available from the data source. Completed candles equal the real NSE bars exactly; price steps inside the forming minute are interpolated between the real open/low/high/close and never leave the real range.">
              · {market.ticks_per_bar} ticks/min · intra-minute path interpolated ⓘ
            </span>
          </span>
        )}
        {market?.mode === 'LIVE' && (
          <span title="Yahoo's public endpoint has no streaming; the charted symbol is polled on a fast lane, the rest in rotation">
            forming candle updates ~every {fmtNum(market.focus_poll_interval_s ?? 2.5, 1)}s · other symbols ~{Math.round(((market.symbols?.length || 1) - 1) * (market.poll_interval_s ?? 4))}s
          </span>
        )}
        <span className="ml-auto">{count} candles · times IST · markers: strategy signals</span>
      </div>
    </div>
  )
}
