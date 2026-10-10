import { useEffect, useRef, useState } from 'react'
import { Maximize2, Minimize2 } from 'lucide-react'
import { IChartApi, IPriceLine, ISeriesApi, LineStyle, LineType, SeriesMarker, Time } from 'lightweight-charts'
import { useEvents } from '../hooks/useEvents'
import { wsClient } from '../services/ws'
import { autoResize, makeChart, signalMarkers, toChartTime } from './base'

interface Pt { time: number; value: number }
const line = (pts: Pt[] = []) => pts.map((p) => ({ time: toChartTime(p.time) as Time, value: p.value }))
const PANE_KEYS = ['macd', 'signal', 'histogram', 'rsi']
// strategies whose chart shows price levels, fill markers and a legend (MACD and RSI + EMA keep their own layout)
const RICH = ['TIME_ENTRY', 'OPEN_BREAKOUT', 'EMA_TREND', 'BREAKOUT']
const LEVEL_STYLE: Record<string, { color: string; style: LineStyle }> = {
  open: { color: '#94a3b8', style: LineStyle.Solid }, buy_trigger: { color: '#22c55e', style: LineStyle.Dashed },
  sell_trigger: { color: '#ef4444', style: LineStyle.Dashed }, entry: { color: '#38bdf8', style: LineStyle.Solid },
  stop_loss: { color: '#f87171', style: LineStyle.Dotted }, take_profit: { color: '#4ade80', style: LineStyle.Dotted },
}

/** Actual executions as markers: fills grouped per candle and side, labelled with quantity and average price. */
function fillMarkers(fills: any[], tfSec: number, first: number | null): SeriesMarker<Time>[] {
  const g = new Map<string, { t: number; side: string; qty: number; notional: number }>()
  for (const f of fills ?? []) {
    const ts = Math.floor(Date.parse(f.at) / 1000)
    const t = ts - (ts % tfSec)
    if (first !== null && t < first) continue
    const k = `${t}:${f.side}`
    const x = g.get(k) ?? { t, side: f.side, qty: 0, notional: 0 }
    x.qty += Number(f.quantity); x.notional += Number(f.quantity) * Number(f.price)
    g.set(k, x)
  }
  return [...g.values()].map((x) => ({
    time: toChartTime(x.t) as Time, position: x.side === 'BUY' ? 'belowBar' : 'aboveBar', shape: x.side === 'BUY' ? 'arrowUp' : 'arrowDown',
    color: x.side === 'BUY' ? '#22c55e' : '#ef4444', text: `${x.side} ${x.qty} @ ${(x.notional / x.qty).toFixed(2)}`, size: 1,
  } as SeriesMarker<Time>))
}

/** Pad an indicator series with whitespace points at every candle time so the candle chart and the
 *  indicator pane share logical indices (indicators start later because of their warm-up). */
function aligned<T extends { time: Time }>(times: number[], pts: Pt[] = [], map: (p: Pt) => T): (T | { time: Time })[] {
  const byTime = new Map(pts.map((p) => [p.time, p]))
  return times.map((t) => { const p = byTime.get(t); return p ? map(p) : { time: toChartTime(t) as Time } })
}

/** Reusable strategy chart: candles + kind-specific overlays and an optional indicator pane. */
export default function StrategyChart({ data, height = 340 }: { data: any; height?: number }) {
  const [expanded, setExpanded] = useState(false)
  const mainEl = useRef<HTMLDivElement>(null)
  const paneEl = useRef<HTMLDivElement>(null)
  const charts = useRef<{ main?: IChartApi; pane?: IChartApi }>({})
  const s = useRef<Record<string, ISeriesApi<any>>>({})
  const lastTime = useRef(0)
  const kind: string = data.kind
  const hasPane = kind === 'MACD' || kind === 'RSI_EMA'

  useEffect(() => {
    if (!mainEl.current) return
    const main = makeChart(mainEl.current, height)
    s.current.candles = main.addCandlestickSeries({ upColor: '#22c55e', downColor: '#ef4444', borderVisible: false, wickUpColor: '#22c55e', wickDownColor: '#ef4444' })
    if (kind === 'EMA_TREND') {
      s.current.ema_fast = main.addLineSeries({ color: '#38bdf8', lineWidth: 2, title: `EMA ${data.params.fast_period}`, priceLineVisible: false, lastValueVisible: false })
      s.current.ema_slow = main.addLineSeries({ color: '#f59e0b', lineWidth: 2, title: `EMA ${data.params.slow_period}`, priceLineVisible: false, lastValueVisible: false })
      s.current.trend_5m = main.addLineSeries({ color: '#a78bfa', lineWidth: 2, lineType: LineType.WithSteps, lineStyle: LineStyle.Dashed,
        title: `5m trend EMA ${data.params.trend_period}`, priceLineVisible: false, lastValueVisible: false })
    }
    if (kind === 'RSI_EMA') {
      s.current.ema_fast = main.addLineSeries({ color: '#38bdf8', lineWidth: 1, title: 'EMA fast', priceLineVisible: false })
      s.current.ema_slow = main.addLineSeries({ color: '#f59e0b', lineWidth: 1, title: 'EMA slow', priceLineVisible: false })
    }
    if (kind === 'BREAKOUT') {
      s.current.resistance = main.addLineSeries({ color: '#ef4444', lineWidth: 1, lineStyle: LineStyle.Dashed, title: 'Resistance', priceLineVisible: false })
      s.current.support = main.addLineSeries({ color: '#22c55e', lineWidth: 1, lineStyle: LineStyle.Dashed, title: 'Support', priceLineVisible: false })
    }
    const offs = [autoResize(main, mainEl.current)]
    charts.current.main = main
    if (hasPane && paneEl.current) {
      const pane = makeChart(paneEl.current, 160)
      if (kind === 'MACD') {
        s.current.histogram = pane.addHistogramSeries({ priceLineVisible: false, title: 'Hist' })
        s.current.macd = pane.addLineSeries({ color: '#38bdf8', lineWidth: 1, title: 'MACD', priceLineVisible: false })
        s.current.signal = pane.addLineSeries({ color: '#f59e0b', lineWidth: 1, title: 'Signal', priceLineVisible: false })
      } else {
        s.current.rsi = pane.addLineSeries({ color: '#a78bfa', lineWidth: 1, title: 'RSI', priceLineVisible: false })
      }
      offs.push(autoResize(pane, paneEl.current))
      charts.current.pane = pane
      let syncing = false
      const sync = (from: IChartApi, to: IChartApi) => from.timeScale().subscribeVisibleLogicalRangeChange((r) => {
        if (syncing || !r) return
        syncing = true; to.timeScale().setVisibleLogicalRange(r); syncing = false
      })
      sync(main, pane); sync(pane, main)
    }
    return () => { offs.forEach((f) => f()); main.remove(); charts.current.pane?.remove(); charts.current = {}; s.current = {} }
  }, [kind, hasPane])

  useEffect(() => {
    const sr = s.current
    if (!sr.candles) return
    const candles = data.candles as any[]
    sr.candles.setData(candles.map((c) => ({ time: toChartTime(c.time) as Time, open: c.open, high: c.high, low: c.low, close: c.close })))
    lastTime.current = candles.length ? candles[candles.length - 1].time : 0
    const first = candles.length ? candles[0].time : null
    const times = candles.map((c) => c.time as number)
    for (const k of ['ema_fast', 'ema_slow', 'trend_5m', 'resistance', 'support']) if (sr[k]) sr[k].setData(line(data.series[k]))
    if (RICH.includes(kind)) {
      // price levels the platform acts on: breakout triggers, entry, stop-loss, target
      ;((sr.candles as any)._levels as IPriceLine[] | undefined)?.forEach((l) => sr.candles.removePriceLine(l))
      ;(sr.candles as any)._levels = (data.levels ?? []).map((l: any) => sr.candles.createPriceLine({
        price: Number(l.price), color: LEVEL_STYLE[l.key]?.color ?? '#94a3b8', lineStyle: LEVEL_STYLE[l.key]?.style ?? LineStyle.Dashed,
        lineWidth: l.key === 'entry' ? 2 : 1, axisLabelVisible: true, title: l.label }))
    }
    for (const k of ['macd', 'signal', 'rsi']) if (sr[k]) sr[k].setData(aligned(times, data.series[k], (p) => ({ time: toChartTime(p.time) as Time, value: p.value })))
    if (sr.histogram) sr.histogram.setData(aligned(times, data.series.histogram, (p) => ({ time: toChartTime(p.time) as Time, value: p.value, color: p.value >= 0 ? 'rgba(34,197,94,.6)' : 'rgba(239,68,68,.6)' })))
    if (sr.rsi) {
      ;(sr.rsi as any)._lines?.forEach((l: any) => sr.rsi.removePriceLine(l))
      ;(sr.rsi as any)._lines = [
        sr.rsi.createPriceLine({ price: data.params.overbought, color: '#ef4444', lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: 'overbought' }),
        sr.rsi.createPriceLine({ price: data.params.oversold, color: '#22c55e', lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: 'oversold' }),
      ]
    }
    if (RICH.includes(kind)) {
      const tfSec = data.timeframe === '5m' ? 300 : 60
      const ms = [...signalMarkers(data.markers.filter((m: any) => !m.action?.startsWith('ORDER')), first), ...fillMarkers(data.fills, tfSec, first)]
      sr.candles.setMarkers(ms.sort((a, b) => (a.time as number) - (b.time as number)))
    } else {
      sr.candles.setMarkers(signalMarkers(data.markers, first))
    }
  }, [data])

  useEffect(() => { wsClient.setSymbols([data.symbol]); return () => wsClient.setSymbols([]) }, [data.symbol])
  // full screen: the price chart takes the window height left after the legend (and the indicator pane)
  useEffect(() => {
    const fit = () => charts.current.main?.applyOptions({ height: expanded ? Math.max(300, window.innerHeight - (hasPane ? 330 : 140)) : height })
    fit()
    if (!expanded) return
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && setExpanded(false)
    window.addEventListener('keydown', esc); window.addEventListener('resize', fit)
    return () => { window.removeEventListener('keydown', esc); window.removeEventListener('resize', fit) }
  }, [expanded, height, hasPane])
  useEvents(['candle.update'], (e) => {
    if (e.symbol !== data.symbol || e.payload.timeframe !== data.timeframe || !s.current.candles) return
    const c = e.payload.candle
    if (c.time < lastTime.current) return
    if (c.time > lastTime.current) {
      // keep the indicator pane index-aligned; real values arrive with the next chart refresh
      for (const k of PANE_KEYS) s.current[k]?.update({ time: toChartTime(c.time) as Time })
    }
    lastTime.current = c.time
    s.current.candles.update({ time: toChartTime(c.time) as Time, open: c.open, high: c.high, low: c.low, close: c.close })
  })

  const legend: [string, string, string][] = kind === 'EMA_TREND'
    ? [['#38bdf8', 'solid', `EMA ${data.params.fast_period} (1m)`], ['#f59e0b', 'solid', `EMA ${data.params.slow_period} (1m)`], ['#a78bfa', 'dashed', `trend EMA ${data.params.trend_period} (5m)`]]
    : kind === 'BREAKOUT' ? [['#ef4444', 'dashed', 'resistance'], ['#22c55e', 'dashed', 'support']] : []
  const notes: Record<string, string> = {
    TIME_ENTRY: `Enters ${data.params.direction === 1 ? 'BUY' : 'SELL'} at 09:15 IST, exits at the 15:15 square-off (no stop-loss / target). Arrows = actual fills.`,
    OPEN_BREAKOUT: `Buys when LTP ≥ open × (1 + ${data.params.breakout_pct}%), sells when LTP ≤ open × (1 − ${data.params.breakout_pct}%); then exits at the ${data.params.take_profit_pct}% target or ${data.params.stop_loss_pct}% stop-loss.`,
    EMA_TREND: 'Enters on a 1m EMA crossover in the direction of the 5m trend EMA; the opposite crossover, stop-loss or target exits.',
    BREAKOUT: 'Enters when a candle closes beyond the resistance / support channel.',
  }
  return (
    <div className={expanded ? 'fixed inset-0 z-50 flex flex-col overflow-auto bg-panel' : 'relative'}>
      <div className={`flex items-center gap-2 ${expanded ? 'border-b border-line px-3 py-2' : 'absolute right-2 top-1.5 z-10'}`}>
        {expanded && <span className="text-sm font-semibold text-white">{data.symbol} · {data.timeframe} · {data.strategy_id}</span>}
        <button className="btn ml-auto px-2 py-1" title={expanded ? 'Exit full screen (Esc)' : 'Full screen'} onClick={() => setExpanded((x) => !x)}>
          {expanded ? <Minimize2 size={13} /> : <Maximize2 size={13} />}</button>
      </div>
      {RICH.includes(kind) && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-line px-3 py-1.5 text-[11px] text-muted">
          {legend.map(([c, st, l]) => <span key={l} className="flex items-center gap-1.5"><span className="inline-block w-4" style={{ borderTop: `2px ${st} ${c}` }} />{l}</span>)}
          {(data.levels ?? []).map((l: any) => <span key={l.key} className="flex items-center gap-1.5"><span className="inline-block w-4" style={{ borderTop: `2px ${LEVEL_STYLE[l.key]?.style === LineStyle.Solid ? 'solid' : 'dashed'} ${LEVEL_STYLE[l.key]?.color}` }} />{l.label} <span className="num text-text">{Number(l.price).toFixed(2)}</span></span>)}
          <span className="flex items-center gap-1.5"><span className="text-pos">▲</span><span className="text-neg">▼</span> fills</span>
          <span className="basis-full text-[10px]">{notes[kind]}</span>
        </div>
      )}
      <div ref={mainEl} className="w-full" />
      {hasPane && <div className="border-t border-line"><div className="px-3 py-1 text-[10px] uppercase tracking-wider text-muted">{kind === 'MACD' ? 'MACD · Signal · Histogram' : 'RSI'}</div><div ref={paneEl} className="w-full" /></div>}
    </div>
  )
}
