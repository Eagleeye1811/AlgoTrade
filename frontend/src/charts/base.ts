import { ColorType, createChart, CrosshairMode, IChartApi, SeriesMarker, Time, UTCTimestamp } from 'lightweight-charts'

// Chart axes are shown in IST (exchange time): shift UTC epoch seconds by +5:30.
export const IST_OFFSET = 19800
export const toChartTime = (t: number) => (t + IST_OFFSET) as UTCTimestamp

export function makeChart(el: HTMLElement, height: number, opts: { timeVisible?: boolean } = {}): IChartApi {
  const chart = createChart(el, {
    height,
    width: el.clientWidth,
    layout: { background: { type: ColorType.Solid, color: '#0f141c' }, textColor: '#7d8a9c', fontSize: 11 },
    grid: { vertLines: { color: '#161e29' }, horzLines: { color: '#161e29' } },
    rightPriceScale: { borderColor: '#1f2a37' },
    timeScale: { borderColor: '#1f2a37', timeVisible: opts.timeVisible ?? true, secondsVisible: false },
    crosshair: { mode: CrosshairMode.Normal },
  })
  return chart
}

export function autoResize(chart: IChartApi, el: HTMLElement): () => void {
  const ro = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth }))
  ro.observe(el)
  return () => ro.disconnect()
}

export interface SignalLike { time: number; signal: string; action?: string; strategy_id?: string }

export function signalMarkers(list: SignalLike[], minTime: number | null, withStrategy = false): SeriesMarker<Time>[] {
  return list
    .filter((s) => minTime === null || s.time >= minTime)
    .map((s) => {
      const traded = s.action?.startsWith('ORDER')
      const buy = s.signal === 'BUY'
      const label = [withStrategy ? s.strategy_id : null, traded ? s.action?.replace('ORDER_', '') : 'no trade'].filter(Boolean).join(' ')
      return {
        time: toChartTime(s.time) as Time,
        position: buy ? 'belowBar' : 'aboveBar',
        color: traded ? (buy ? '#22c55e' : '#ef4444') : '#64748b',
        shape: buy ? 'arrowUp' : 'arrowDown',
        text: `${s.signal} ${label}`,
        size: traded ? 1 : 0.6,
      } as SeriesMarker<Time>
    })
    .sort((a, b) => (a.time as number) - (b.time as number))
}
