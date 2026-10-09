import { useState } from 'react'
import { Download } from 'lucide-react'
import { useApi } from '../hooks/useEvents'
import { api } from '../services/api'
import { fmtINR, fmtNum, fmtQty, fmtSigned, pnlClass } from '../services/format'
import { Empty, ErrorText, Loading, Panel, Stat } from '../components/ui'

const todayIST = () => new Date(Date.now() + 19800000).toISOString().slice(0, 10)
const COMPONENTS = ['brokerage', 'stt', 'exchange_txn', 'sebi', 'stamp_duty', 'gst'] as const

/** L2: the exact list of trades (fills) with gross P&L, charges from the published table and net P&L. */
export default function Trades() {
  const [day, setDay] = useState(todayIST())
  const [all, setAll] = useState(false)
  const [mine, setMine] = useState(false)
  const qs = `${all ? 'all_days=true' : `date=${day}`}${mine ? '&mine=true' : ''}`
  const d = useApi<any>(`/trades?${qs}`, ['order.update', 'position.update'], 1500)
  const [err, setErr] = useState<string | null>(null)

  const download = async () => {
    setErr(null)
    try {
      const text = await api.get<string>(`/trades.csv?${qs}`)
      const url = URL.createObjectURL(new Blob([text], { type: 'text/csv' }))
      const a = document.createElement('a')
      a.href = url; a.download = `trades-${all ? 'all' : day}.csv`; a.click()
      URL.revokeObjectURL(url)
    } catch (e: any) { setErr(e.message) }
  }

  const t = d.data?.total
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-base font-semibold">Trades &amp; net P&amp;L</h1>
        <input type="date" className="input" value={day} disabled={all} onChange={(e) => setDay(e.target.value)} />
        <label className="flex items-center gap-1.5 text-xs text-muted"><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> all days</label>
        <label className="flex items-center gap-1.5 text-xs text-muted"><input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} /> only my subscriptions</label>
        <button className="btn ml-auto" onClick={download}><Download size={14} /> CSV</button>
      </div>
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
        <Panel title={`Trade list (${d.data.trades.length})`}>
          {d.data.trades.length === 0 ? <Empty>No trades in this period</Empty> : (
            <div className="overflow-x-auto"><table className="tbl">
              <thead><tr><th>Time (IST)</th><th>Strategy</th><th>Symbol</th><th>Side</th><th className="text-right">Qty</th><th className="text-right">Price</th>
                <th className="text-right">Turnover</th><th className="text-right">Gross P&amp;L</th><th className="text-right">Charges</th><th className="text-right">Net</th><th>Order</th></tr></thead>
              <tbody>{d.data.trades.map((x: any) => (
                <tr key={x.fill_id}><td className="num text-muted">{x.time.slice(0, 19).replace('T', ' ')}</td><td>{x.strategy_id}</td><td>{x.symbol}</td>
                  <td className={x.side === 'BUY' ? 'text-pos' : 'text-neg'}>{x.side}</td><td className="num text-right">{fmtQty(x.quantity)}</td>
                  <td className="num text-right">{fmtNum(x.price)}</td><td className="num text-right">{fmtINR(x.turnover)}</td>
                  <td className={`num text-right ${pnlClass(x.realized_gross)}`}>{Number(x.realized_gross) ? fmtSigned(x.realized_gross, 2) : '—'}</td>
                  <td className="num text-right text-amber-300" title={COMPONENTS.map((k) => `${k}: ${x.charges[k]}`).join('\n')}>{fmtNum(x.charges.total)}</td>
                  <td className={`num text-right ${pnlClass(x.net)}`}>{fmtSigned(x.net, 2)}</td>
                  <td className="num text-muted">{x.order_id.slice(0, 8)}</td></tr>))}</tbody>
            </table></div>
          )}
        </Panel>
      </>}
    </div>
  )
}
