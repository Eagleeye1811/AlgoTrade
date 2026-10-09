import { useEffect, useRef, useState } from 'react'
import { IChartApi, ISeriesApi, Time } from 'lightweight-charts'
import { api } from '../services/api'
import { wsClient } from '../services/ws'
import { useEvents } from '../hooks/useEvents'
import { fmtNum } from '../services/format'
import { StatusBadge } from '../components/ui'
import { useSystem } from '../hooks/system'
import { useAuth } from '../hooks/auth'
import { autoResize, makeChart, signalMarkers, toChartTime } from './base'

interface Candle { time: number; open: number; high: number; low: number; close: number; volume: number }

export default function MarketChart({ symbols, height = 380 }: { symbols: string[]; height?: number }) {
  const [symbol, setSymbol] = useState<string>('')
  const [tf, setTf] = useState<'1m' | '5m'>('1m')
  const [ltp, setLtp] = useState<number | null>(null)
  const [status, setStatus] = useState<string | null>(null)
  const [count, setCount] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const el = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const cs = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const vs = useRef<ISeriesApi<'Histogram'> | null>(null)
  const lastTime = useRef<number>(0)
  const firstTime = useRef<number | null>(null)
  const { overview, reload } = useSystem()
  const { user } = useAuth()
  const market = overview?.market
  const isReplay = market?.mode === 'REPLAY'
  const setSpeed = async (speed: number) => {
    try { await api.post('/market/replay', { speed }); reload() } catch (e: any) { setError(e.message) }
  }

  useEffect(() => { if (!symbol && symbols.length) setSymbol(symbols[0]) }, [symbols, symbol])

  useEffect(() => {
    if (!el.current) return
    const c = makeChart(el.current, height)
    cs.current = c.addCandlestickSeries({ upColor: '#22c55e', downColor: '#ef4444', borderVisible: false, wickUpColor: '#22c55e', wickDownColor: '#ef4444' })
    vs.current = c.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol' })
    c.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } })
    chart.current = c
    const off = autoResize(c, el.current)
    return () => { off(); c.remove(); chart.current = null }
  }, [height])

  const load = async (sym: string, timeframe: string, keepView = false) => {
    try {
      const d = await api.get(`/market/candles/${sym}?timeframe=${timeframe}&limit=400`)
      const candles: Candle[] = d.candles
      setError(null); setStatus(d.status); setCount(candles.length)
      cs.current?.setData(candles.map((c) => ({ time: toChartTime(c.time) as Time, open: c.open, high: c.high, low: c.low, close: c.close })))
      vs.current?.setData(candles.map((c) => ({ time: toChartTime(c.time) as Time, value: c.volume, color: c.close >= c.open ? 'rgba(34,197,94,.35)' : 'rgba(239,68,68,.35)' })))
      lastTime.current = candles.length ? candles[candles.length - 1].time : 0
      firstTime.current = candles.length ? candles[0].time : null
      if (candles.length) setLtp(candles[candles.length - 1].close)
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
    lastTime.current = c.time
    cs.current?.update({ time: toChartTime(c.time) as Time, open: c.open, high: c.high, low: c.low, close: c.close })
    vs.current?.update({ time: toChartTime(c.time) as Time, value: c.volume, color: c.close >= c.open ? 'rgba(34,197,94,.35)' : 'rgba(239,68,68,.35)' })
    setLtp(c.close)
  })
  useEvents(['market.tick'], (e) => { if (e.symbol === symbol) setLtp(e.payload.price) })
  useEvents(['market.status'], (e) => setStatus(e.payload.display_status))
  const refreshMarkers = useRef<number | null>(null)
  useEvents(['strategy.signal'], (e) => {
    if (e.symbol !== symbol || refreshMarkers.current) return
    refreshMarkers.current = window.setTimeout(() => { refreshMarkers.current = null; load(symbol, tf, true) }, 600)
  })

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-line">
        <select className="input" value={symbol} onChange={(e) => setSymbol(e.target.value)}>
          {symbols.map((s) => <option key={s}>{s}</option>)}
        </select>
        <div className="flex overflow-hidden rounded border border-line">
          {(['1m', '5m'] as const).map((x) => <button key={x} onClick={() => setTf(x)} className={`px-2 py-1 text-xs ${tf === x ? 'bg-info text-white' : 'text-muted hover:text-text'}`}>{x}</button>)}
        </div>
        <span className="text-xs text-muted">NSE</span>
        <span className="num text-lg font-semibold text-white">{ltp !== null ? fmtNum(ltp) : '—'}</span>
        <StatusBadge status={status} />
        {isReplay && (
          <span className="flex items-center gap-1 text-[11px] text-muted">
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
        {market?.mode === 'LIVE' && <span className="text-[11px] text-muted">live poll ~{Math.round((market.symbols?.length || 1) * 4)}s per symbol</span>}
        <span className="ml-auto text-[11px] text-muted">{count} candles · times IST · markers: strategy signals</span>
      </div>
      {error && <div className="px-3 py-1 text-xs text-red-400">{error}</div>}
      <div ref={el} className="w-full" />
    </div>
  )
}
