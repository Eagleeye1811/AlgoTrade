const inr0 = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 })
const inr2 = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', minimumFractionDigits: 2, maximumFractionDigits: 2 })
const num = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 })

export const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
export function toNum(v: unknown): number | null { if (v === null || v === undefined || v === '') return null; const n = Number(v); return Number.isFinite(n) ? n : null }
export function fmtINR(v: unknown, decimals = 0): string { const n = toNum(v); return n === null ? '—' : (decimals ? inr2 : inr0).format(n) }
export function fmtNum(v: unknown, d = 2): string { const n = toNum(v); return n === null ? '—' : new Intl.NumberFormat('en-IN', { maximumFractionDigits: d }).format(n) }
export function fmtQty(v: unknown): string { const n = toNum(v); return n === null ? '—' : num.format(n) }
export function fmtSigned(v: unknown, decimals = 0): string { const n = toNum(v); if (n === null) return '—'; return (n > 0 ? '+' : '') + fmtINR(n, decimals) }
export function pnlClass(v: unknown) { const n = toNum(v); return n === null || n === 0 ? 'text-text' : n > 0 ? 'text-pos' : 'text-neg' }
export function fmtTime(iso?: string | null, withDate = false): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return '—'
  return d.toLocaleString('en-IN', { hour12: false, ...(withDate ? { day: '2-digit', month: 'short' } : {}), hour: '2-digit', minute: '2-digit', second: '2-digit' })
}
export function fmtMarketTime(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false, day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) + ' IST'
}
export const shortId = (id?: string | null) => (id ? id.slice(0, 8) : '—')
