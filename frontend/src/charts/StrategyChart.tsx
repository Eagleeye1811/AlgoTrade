import { useEffect, useRef } from 'react'
import { IChartApi, ISeriesApi, LineStyle, Time } from 'lightweight-charts'
import { useEvents } from '../hooks/useEvents'
import { wsClient } from '../services/ws'
import { autoResize, makeChart, signalMarkers, toChartTime } from './base'

interface Pt { time: number; value: number }
const line = (pts: Pt[] = []) => pts.map((p) => ({ time: toChartTime(p.time) as Time, value: p.value }))
const PANE_KEYS = ['macd', 'signal', 'histogram', 'rsi']

/** Pad an indicator series with whitespace points at every candle time so the candle chart and the
 *  indicator pane share logical indices (indicators start later because of their warm-up). */
function aligned<T extends { time: Time }>(times: number[], pts: Pt[] = [], map: (p: Pt) => T): (T | { time: Time })[] {
  const byTime = new Map(pts.map((p) => [p.time, p]))
  return times.map((t) => { const p = byTime.get(t); return p ? map(p) : { time: toChartTime(t) as Time } })
}

/** Reusable strategy chart: candles + kind-specific overlays and an optional indicator pane. */
export default function StrategyChart({ data }: { data: any }) {
  const mainEl = useRef<HTMLDivElement>(null)
  const paneEl = useRef<HTMLDivElement>(null)
  const charts = useRef<{ main?: IChartApi; pane?: IChartApi }>({})
  const s = useRef<Record<string, ISeriesApi<any>>>({})
  const lastTime = useRef(0)
  const kind: string = data.kind
  const hasPane = kind === 'MACD' || kind === 'RSI_EMA'

  useEffect(() => {
    if (!mainEl.current) return
    const main = makeChart(mainEl.current, 340)
    s.current.candles = main.addCandlestickSeries({ upColor: '#22c55e', downColor: '#ef4444', borderVisible: false, wickUpColor: '#22c55e', wickDownColor: '#ef4444' })
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
    for (const k of ['ema_fast', 'ema_slow', 'resistance', 'support']) if (sr[k]) sr[k].setData(line(data.series[k]))
    for (const k of ['macd', 'signal', 'rsi']) if (sr[k]) sr[k].setData(aligned(times, data.series[k], (p) => ({ time: toChartTime(p.time) as Time, value: p.value })))
    if (sr.histogram) sr.histogram.setData(aligned(times, data.series.histogram, (p) => ({ time: toChartTime(p.time) as Time, value: p.value, color: p.value >= 0 ? 'rgba(34,197,94,.6)' : 'rgba(239,68,68,.6)' })))
    if (sr.rsi) {
      ;(sr.rsi as any)._lines?.forEach((l: any) => sr.rsi.removePriceLine(l))
      ;(sr.rsi as any)._lines = [
        sr.rsi.createPriceLine({ price: data.params.overbought, color: '#ef4444', lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: 'overbought' }),
        sr.rsi.createPriceLine({ price: data.params.oversold, color: '#22c55e', lineStyle: LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title: 'oversold' }),
      ]
    }
    sr.candles.setMarkers(signalMarkers(data.markers, first))
  }, [data])

  useEffect(() => { wsClient.setSymbols([data.symbol]); return () => wsClient.setSymbols([]) }, [data.symbol])
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

  return (
    <div>
      <div ref={mainEl} className="w-full" />
      {hasPane && <div className="border-t border-line"><div className="px-3 py-1 text-[10px] uppercase tracking-wider text-muted">{kind === 'MACD' ? 'MACD · Signal · Histogram' : 'RSI'}</div><div ref={paneEl} className="w-full" /></div>}
    </div>
  )
}
