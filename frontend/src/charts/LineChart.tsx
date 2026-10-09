import { useEffect, useRef } from 'react'
import { IChartApi, ISeriesApi, Time } from 'lightweight-charts'
import { autoResize, makeChart, toChartTime } from './base'

export interface LineDef { key: string; title: string; color: string }

export default function LineChart({ rows, lines, height = 220 }: { rows: any[]; lines: LineDef[]; height?: number }) {
  const el = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const series = useRef<Record<string, ISeriesApi<'Line'>>>({})
  const sig = lines.map((l) => l.key).join(',')
  useEffect(() => {
    if (!el.current) return
    const c = makeChart(el.current, height)
    lines.forEach((l) => { series.current[l.key] = c.addLineSeries({ color: l.color, lineWidth: 2, title: l.title, priceLineVisible: false }) })
    chart.current = c
    const off = autoResize(c, el.current)
    return () => { off(); c.remove(); series.current = {} }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig, height])
  useEffect(() => {
    const seen = new Set<number>()
    const clean = rows.filter((r) => { if (seen.has(r.time)) return false; seen.add(r.time); return true }).sort((a, b) => a.time - b.time)
    lines.forEach((l) => series.current[l.key]?.setData(clean.map((r) => ({ time: toChartTime(r.time) as Time, value: Number(r[l.key]) }))))
    chart.current?.timeScale().fitContent()
  }, [rows, sig])
  return <div ref={el} className="w-full" />
}
