import { useEffect, useState } from 'react'
import { Sparkles } from 'lucide-react'
import { api } from '../services/api'
import { fmtINR } from '../services/format'
import { ErrorText, Modal } from './ui'

/** Subscribe the current user to a strategy on one symbol, with its own allocation and risk limits. */
export default function SubscribeModal({ open, onClose, onDone }: { open: boolean; onClose: () => void; onDone: () => void }) {
  const [cat, setCat] = useState<any>(null)
  const [recs, setRecs] = useState<any>(null)
  const [kind, setKind] = useState('')
  const [symbol, setSymbol] = useState('')
  const [qty, setQty] = useState('10')
  const [alloc, setAlloc] = useState('')
  const [params, setParams] = useState<Record<string, string>>({})
  const [limits, setLimits] = useState({ max_daily_loss: '', max_position: '', max_orders_per_min: '' })
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    setErr(null)
    api.get('/strategies/catalog').then((c) => {
      setCat(c)
      const first = c.strategies[0]
      setKind(first?.kind ?? ''); setSymbol(c.symbols[0] ?? '')
      setAlloc('')
    }).catch((e) => setErr(e.message))

    api.get('/risk/recommendations').then((r) => {
      setRecs(r)
    }).catch(() => {})
  }, [open])

  const applySuggestedLimits = () => {
    if (!recs) return
    const recStrat = recs.strategies[kind] || Object.values(recs.strategies)[0] as any
    if (recStrat) {
      setAlloc(String(recStrat.allocation))
      setQty(String(recStrat.max_position <= 25 ? 10 : 25))
      setLimits({
        max_daily_loss: String(recStrat.max_daily_loss),
        max_position: String(recStrat.max_position),
        max_orders_per_min: String(recStrat.max_orders_per_min),
      })
    }
  }

  const spec = cat?.strategies.find((s: any) => s.kind === kind)
  useEffect(() => { if (spec) setParams(Object.fromEntries(spec.param_specs.map((p: any) => [p.name, String(p.default)]))) }, [kind, cat])

  const submit = async () => {
    setBusy(true); setErr(null)
    try {
      const lim = Object.fromEntries(Object.entries(limits).filter(([, v]) => v !== '').map(([k, v]) => [k, Number(v)]))
      await api.post('/strategies', {
        kind, symbol, quantity: Number(qty), allocation: alloc === '' ? 0 : Number(alloc),
        params: Object.fromEntries(Object.entries(params).map(([k, v]) => [k, Number(v)])), ...lim,
      })
      onDone(); onClose()
    } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }

  return (
    <Modal open={open} onClose={onClose} wide title="Subscribe to a strategy"
      footer={<><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" disabled={busy || !cat?.account_configured || !kind || !symbol} onClick={submit}>Subscribe</button></>}>
      {!cat ? <div className="text-xs text-muted">{err ?? 'Loading…'}</div> : <>
        {!cat.account_configured && <div className="text-xs text-amber-400">The trading account is not configured yet — an ADMIN must complete Account Setup first.</div>}
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="text-xs text-muted">Strategy
            <select className="input mt-1 w-full" value={kind} onChange={(e) => setKind(e.target.value)}>
              {cat.strategies.map((s: any) => <option key={s.kind} value={s.kind}>{s.name}</option>)}
            </select></label>
          <label className="text-xs text-muted">Symbol (NSE)
            <select className="input mt-1 w-full" value={symbol} onChange={(e) => setSymbol(e.target.value)}>
              {cat.symbols.map((s: string) => <option key={s}>{s}</option>)}
            </select></label>
        </div>
        {spec && <p className="text-xs text-muted">{spec.description}</p>}
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="text-xs text-muted">Order quantity (shares, fixed per entry)
            <input className="input mt-1 w-full num" type="number" min={1} step={1} value={qty} onChange={(e) => setQty(e.target.value)} /></label>
          <label className="text-xs text-muted">Allocation / virtual margin (INR, optional) · {fmtINR(cat.unallocated_capital)} free
            <input className="input mt-1 w-full num" type="number" min={0} placeholder="set later with Configure" value={alloc} onChange={(e) => setAlloc(e.target.value)} /></label>
        </div>
        <div>
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-muted">Risk limits (enforced by the platform)</span>
            {recs && (
              <button
                type="button"
                onClick={applySuggestedLimits}
                className="flex items-center gap-1 text-[11px] text-amber-300 hover:text-amber-200 underline decoration-dotted"
              >
                <Sparkles size={12} className="text-amber-400" />
                Auto-fill AI Suggested Limits
              </button>
            )}
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            <label className="text-xs text-muted">Max loss for the day (INR)
              <input className="input mt-1 w-full num" type="number" min={1} placeholder="5% of allocation" value={limits.max_daily_loss}
                onChange={(e) => setLimits({ ...limits, max_daily_loss: e.target.value })} /></label>
            <label className="text-xs text-muted">Max position (shares)
              <input className="input mt-1 w-full num" type="number" min={1} placeholder="500" value={limits.max_position}
                onChange={(e) => setLimits({ ...limits, max_position: e.target.value })} /></label>
            <label className="text-xs text-muted">Max orders per minute
              <input className="input mt-1 w-full num" type="number" min={1} placeholder="15" value={limits.max_orders_per_min}
                onChange={(e) => setLimits({ ...limits, max_orders_per_min: e.target.value })} /></label>
          </div>
        </div>
        {spec && (
          <div>
            <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-muted">Parameters</div>
            <div className="grid gap-3 sm:grid-cols-3">
              {spec.param_specs.map((p: any) => (
                <label key={p.name} className="text-xs text-muted">{p.label} <span className="text-[10px]">({p.min}–{p.max})</span>
                  <input className="input mt-1 w-full num" type="number" step={p.type === 'int' ? 1 : 0.01} min={p.min} max={p.max}
                    value={params[p.name] ?? ''} onChange={(e) => setParams({ ...params, [p.name]: e.target.value })} /></label>
              ))}
            </div>
          </div>
        )}
        <ErrorText error={err} />
      </>}
    </Modal>
  )
}
