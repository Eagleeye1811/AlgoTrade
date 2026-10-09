import { useState } from 'react'
import { CalendarDays, ChevronLeft, ChevronRight, Download } from 'lucide-react'
import { useApi } from '../hooks/useEvents'
import { api } from '../services/api'
import { fmtINR, fmtNum, fmtQty, fmtSigned, pnlClass } from '../services/format'
import { Empty, ErrorText, Loading, Panel, Stat } from './ui'

const todayIST = () => new Date(Date.now() + 19800000).toISOString().slice(0, 10)
const shiftDay = (d: string, n: number) => new Date(Date.parse(`${d}T00:00:00Z`) + n * 86400000).toISOString().slice(0, 10)
const fmtDay = (d: string) => new Date(`${d}T00:00:00Z`).toLocaleDateString('en-IN', { timeZone: 'UTC', weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' })
export const COMPONENTS = ['brokerage', 'stt', 'exchange_txn', 'sebi', 'stamp_duty', 'gst'] as const

/** Period filter shared by the trades views: all days (default) or one IST day, optionally only my subscriptions. */
export function useTradeFilter() {
  const [day, setDay] = useState(todayIST())
  const [all, setAll] = useState(true)
  const [mine, setMine] = useState(false)
  const qs = `${all ? 'all_days=true' : `date=${day}`}${mine ? '&mine=true' : ''}`
  return { day, setDay, all, setAll, mine, setMine, qs }
}
export type TradeFilter = ReturnType<typeof useTradeFilter>

export async function downloadTradesCsv(f: TradeFilter) {
  const text = await api.get<string>(`/trades.csv?${f.qs}`)
  const url = URL.createObjectURL(new Blob([text], { type: 'text/csv' }))
  const a = document.createElement('a')
  a.href = url; a.download = `trades-${f.all ? 'all' : f.day}.csv`; a.click()
  URL.revokeObjectURL(url)
}

export function TradeFilterBar({ f, title, onCsv }: { f: TradeFilter; title: string; onCsv: () => void }) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <h1 className="text-base font-semibold">{title}</h1>
      <div className="flex overflow-hidden rounded-md border border-line bg-bg p-0.5 text-xs">
        <button onClick={() => f.setAll(true)} className={`rounded px-3 py-1 font-medium ${f.all ? 'bg-info text-white' : 'text-muted hover:text-text'}`}>All days</button>
        <button onClick={() => f.setAll(false)} className={`flex items-center gap-1 rounded px-3 py-1 font-medium ${!f.all ? 'bg-info text-white' : 'text-muted hover:text-text'}`}><CalendarDays size={12} /> Pick a date</button>
      </div>
      {!f.all && (
        <div className="flex items-center gap-1">
          <button className="btn px-2" title="Previous day" onClick={() => f.setDay(shiftDay(f.day, -1))}><ChevronLeft size={14} /></button>
          {/* native calendar picker; opens on click anywhere in the field */}
          <input type="date" className="input num" style={{ colorScheme: 'dark' }} value={f.day} max={todayIST()}
            onClick={(e) => { try { (e.currentTarget as any).showPicker?.() } catch { /* not supported */ } }}
            onChange={(e) => e.target.value && f.setDay(e.target.value)} />
          <button className="btn px-2" title="Next day" disabled={f.day >= todayIST()} onClick={() => f.setDay(shiftDay(f.day, 1))}><ChevronRight size={14} /></button>
          {f.day !== todayIST() && <button className="btn" onClick={() => f.setDay(todayIST())}>Today</button>}
          <span className="text-xs text-muted">{fmtDay(f.day)}</span>
        </div>
      )}
      <label className="flex items-center gap-1.5 text-xs text-muted"><input type="checkbox" checked={f.mine} onChange={(e) => f.setMine(e.target.checked)} /> only my subscriptions</label>
      <button className="btn ml-auto" onClick={onCsv}><Download size={14} /> CSV</button>
    </div>
  )
}

/** L2: net P&L after charges - totals and per strategy - for the selected period. */
export default function TradesSummary({ f, title = 'Trades & net P&L' }: { f: TradeFilter; title?: string }) {
  const d = useApi<any>(`/trades?${f.qs}`, ['order.update', 'position.update'], 1500)
  const [err, setErr] = useState<string | null>(null)
  const t = d.data?.total
  return (
    <div className="space-y-3">
      <TradeFilterBar f={f} title={title} onCsv={() => { setErr(null); downloadTradesCsv(f).catch((e) => setErr(e.message)) }} />
      <ErrorText error={err ?? d.error} />
      {!d.data ? <Loading /> : <>
        <div className="text-[11px] text-muted">Charges: {d.data.charges_table}{d.data.charges_source ? ` — ${d.data.charges_source}` : ''}</div>
        <Panel bodyClass="grid grid-cols-2 sm:grid-cols-5 divide-x divide-line">
          <Stat label="Trades (fills)" value={t.trades} />
          <Stat label="Turnover" value={fmtINR(t.turnover)} />
          <Stat label="Gross realized P&L" value={fmtSigned(t.realized_gross, 2)} className={pnlClass(t.realized_gross)} />
          <Stat label="Charges" value={fmtINR(t.charges, 2)} className="text-amber-300" />
          <Stat label="Net realized P&L" value={fmtSigned(t.net_realized, 2)} className={pnlClass(t.net_realized)} />
        </Panel>
        <Panel title="Per strategy">
          {d.data.strategies.length === 0 ? <Empty>No trades in this period</Empty> : (
            <div className="overflow-x-auto"><table className="tbl">
              <thead><tr><th>Strategy</th><th className="text-right">Trades</th><th className="text-right">Buy / sell qty</th><th className="text-right">Turnover</th>
                <th className="text-right">Gross</th>{COMPONENTS.map((k) => <th key={k} className="text-right">{k.replace('_', ' ')}</th>)}
                <th className="text-right">Charges</th><th className="text-right">Net</th><th className="text-right">Open qty</th></tr></thead>
              <tbody>{d.data.strategies.map((s: any) => (
                <tr key={s.strategy_id}><td className="font-medium">{s.strategy_id}</td><td className="num text-right">{s.trades}</td>
                  <td className="num text-right">{fmtQty(s.buy_qty)} / {fmtQty(s.sell_qty)}</td><td className="num text-right">{fmtINR(s.turnover)}</td>
                  <td className={`num text-right ${pnlClass(s.realized_gross)}`}>{fmtSigned(s.realized_gross, 2)}</td>
                  {COMPONENTS.map((k) => <td key={k} className="num text-right text-muted">{fmtNum(s[`charges_${k}`])}</td>)}
                  <td className="num text-right text-amber-300">{fmtINR(s.charges, 2)}</td>
                  <td className={`num text-right font-semibold ${pnlClass(s.net_realized)}`}>{fmtSigned(s.net_realized, 2)}</td>
                  <td className="num text-right">{fmtQty(s.open_quantity)}</td></tr>))}</tbody>
            </table></div>
          )}
        </Panel>
      </>}
    </div>
  )
}

/** L2: the exact list of trades (fills) of the period, each with gross P&L, its share of the charges and net P&L. */
export function TradeList({ f }: { f: TradeFilter }) {
  const d = useApi<any>(`/trades?${f.qs}`, ['order.update', 'position.update'], 1500)
  if (!d.data) return d.error ? <ErrorText error={d.error} /> : <Loading />
  return (
    <Panel title={`Trade list (${d.data.trades.length})`}>
          {d.data.trades.length === 0 ? <Empty>No trades in this period</Empty> : (
            <div className="overflow-x-auto"><table className="tbl">
              <thead><tr><th>Time (IST)</th><th>Strategy</th><th>Symbol</th><th>Side</th><th className="text-right">Qty</th><th className="text-right">Price</th>
                <th className="text-right">Turnover</th><th className="text-right">Gross P&amp;L</th><th className="text-right">Charges</th><th className="text-right">Net</th>
                <th className="text-right" title="fill price vs the price when the order was sent">Slippage</th><th className="text-right">Latency</th><th>Source</th><th>Order</th></tr></thead>
              <tbody>{d.data.trades.map((x: any) => (
                <tr key={x.fill_id}><td className="num text-muted">{x.time.slice(0, 19).replace('T', ' ')}</td><td>{x.strategy_id}</td><td>{x.symbol}</td>
                  <td className={x.side === 'BUY' ? 'text-pos' : 'text-neg'}>{x.side}</td><td className="num text-right">{fmtQty(x.quantity)}</td>
                  <td className="num text-right">{fmtNum(x.price)}</td><td className="num text-right">{fmtINR(x.turnover)}</td>
                  <td className={`num text-right ${pnlClass(x.realized_gross)}`}>{Number(x.realized_gross) ? fmtSigned(x.realized_gross, 2) : '—'}</td>
                  <td className="num text-right text-amber-300" title={COMPONENTS.map((k) => `${k}: ${x.charges[k]}`).join('\n')}>{fmtNum(x.charges.total)}</td>
                  <td className={`num text-right ${pnlClass(x.net)}`}>{fmtSigned(x.net, 2)}</td>
                  <td className="num text-right text-muted">{x.slippage_bps != null ? `${fmtNum(x.slippage_bps, 1)} bps` : '—'}</td>
                  <td className="num text-right text-muted">{x.latency_ms != null ? `${x.latency_ms} ms` : '—'}</td>
                  <td>{x.source === 'RECONCILIATION' ? <span className="rounded border border-amber-500/40 px-1 text-[10px] text-amber-300" title="recovered from 021 by reconciliation (the execution report was lost)">recon</span> : <span className="text-muted">report</span>}</td>
                  <td className="num text-muted">{x.order_id.slice(0, 8)}</td></tr>))}</tbody>
            </table></div>
          )}
        </Panel>
  )
}
