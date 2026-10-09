import { useEffect, useState } from 'react'
import { api } from '../services/api'
import { ErrorText, Modal } from './ui'

export default function StrategyConfigModal({ strategy, open, onClose, onSaved }: { strategy: any; open: boolean; onClose: () => void; onSaved: () => void }) {
  const [params, setParams] = useState<Record<string, string>>({})
  const [qty, setQty] = useState('')
  const [alloc, setAlloc] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (!open || !strategy) return
    setParams(Object.fromEntries(Object.entries(strategy.params).map(([k, v]) => [k, String(v)])))
    setQty(String(strategy.quantity)); setAlloc(String(strategy.allocation)); setErr(null)
  }, [open, strategy])
  if (!strategy) return null
  const invalid = strategy.param_specs.find((s: any) => {
    const v = Number(params[s.name]); return params[s.name] === '' || isNaN(v) || v < s.min || v > s.max || (s.type === 'int' && !Number.isInteger(v))
  })
  const save = async () => {
    setBusy(true); setErr(null)
    try {
      await api.put(`/strategies/${strategy.id}/config`, {
        params: Object.fromEntries(Object.entries(params).map(([k, v]) => [k, Number(v)])),
        quantity: Number(qty), allocation: Number(alloc),
      })
      onSaved(); onClose()
    } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  return (
    <Modal open={open} onClose={onClose} title={`Configure ${strategy.name}`}
      footer={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn btn-primary" disabled={busy || !!invalid} onClick={save}>Save new version</button></>}>
      <p className="text-xs text-muted">Saving creates config v{strategy.config_version + 1}; indicators are re-warmed from candle history. Changes are audited.</p>
      <div className="grid grid-cols-2 gap-3">
        {strategy.param_specs.map((s: any) => (
          <label key={s.name} className="text-xs text-muted">{s.label} <span className="text-[10px]">({s.min}–{s.max})</span>
            <input className="input mt-1 w-full num" type="number" step={s.type === 'int' ? 1 : 0.01} min={s.min} max={s.max} value={params[s.name] ?? ''}
              onChange={(e) => setParams({ ...params, [s.name]: e.target.value })} /></label>
        ))}
        <label className="text-xs text-muted">Order quantity (shares)<input className="input mt-1 w-full num" type="number" min={1} step={1} value={qty} onChange={(e) => setQty(e.target.value)} /></label>
        <label className="text-xs text-muted">Allocation / virtual margin (INR)<input className="input mt-1 w-full num" type="number" min={0} value={alloc} onChange={(e) => setAlloc(e.target.value)} /></label>
      </div>
      {invalid && <div className="text-xs text-amber-400">{invalid.label} must be {invalid.type} within {invalid.min}–{invalid.max}</div>}
      <ErrorText error={err} />
    </Modal>
  )
}
