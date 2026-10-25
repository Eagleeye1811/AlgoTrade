import { useEffect, useState } from 'react'
import { Sparkles, Check, RefreshCw, UserCheck, ShieldAlert, TrendingUp } from 'lucide-react'
import { api } from '../services/api'
import { fmtINR } from '../services/format'
import { Badge, ErrorText, Panel } from './ui'

interface RiskAdvisorProps {
  currentLimits?: any
  isAdmin?: boolean
  onLimitsApplied?: () => void
}

export default function RiskAdvisor({ isAdmin = false, onLimitsApplied }: RiskAdvisorProps) {
  const [age, setAge] = useState<number>(21)
  const [income, setIncome] = useState<number>(0)
  const [incomeText, setIncomeText] = useState('0')
  // typed salary: recalculate shortly after the user stops typing
  useEffect(() => {
    const v = Math.max(0, Math.round(Number(incomeText) || 0))
    const id = window.setTimeout(() => setIncome(v), 400)
    return () => clearTimeout(id)
  }, [incomeText])
  const [appetite, setAppetite] = useState<string>('AGGRESSIVE')
  const [recs, setRecs] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  const [applying, setApplying] = useState(false)
  const [applySuccess, setApplySuccess] = useState(false)
  const [applyAllocations, setApplyAllocations] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const fetchRecs = async () => {
    setLoading(true)
    setErr(null)
    try {
      const data = await api.get(`/risk/recommendations?age=${age}&income=${income}&risk_appetite=${appetite}`)
      setRecs(data)
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchRecs()
  }, [age, income, appetite])

  const applyPreset = (pAge: number, pIncome: number, pAppetite: string) => {
    setAge(pAge)
    setIncome(pIncome)
    setIncomeText(String(pIncome))
    setAppetite(pAppetite)
  }

  const handleApply = async () => {
    if (!isAdmin) return
    setApplying(true)
    setErr(null)
    try {
      await api.post('/risk/apply-recommendations', {
        age,
        income,
        risk_appetite: appetite,
        apply_allocations: applyAllocations,
      })
      setApplySuccess(true)
      setTimeout(() => setApplySuccess(false), 4000)
      if (onLimitsApplied) onLimitsApplied()
    } catch (e: any) {
      setErr(e.message)
    } finally {
      setApplying(false)
    }
  }

  return (
    <Panel
      title={
        <span className="flex items-center gap-2 text-sm font-medium text-amber-300">
          <Sparkles size={16} className="text-amber-400 animate-pulse" />
          AI Risk Advisor &amp; Profile Recommendations
        </span>
      }
      right={
        <div className="flex items-center gap-2">
          {recs && (
            <Badge tone={recs.risk_score >= 80 ? 'purple' : recs.risk_score >= 50 ? 'blue' : 'gray'}>
              Risk Score: {recs.risk_score}/100 · {recs.risk_tier}
            </Badge>
          )}
        </div>
      }
      bodyClass="p-4 space-y-4"
    >
      <ErrorText error={err} />

      {/* Preset Quick-Buttons */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold text-muted">Quick Presets:</span>
        <button
          type="button"
          onClick={() => applyPreset(21, 0, 'AGGRESSIVE')}
          className={`btn text-xs py-1 px-2.5 flex items-center gap-1.5 ${
            age === 21 && appetite === 'AGGRESSIVE' ? 'border-amber-400 bg-amber-500/20 text-amber-200' : ''
          }`}
        >
          <UserCheck size={13} className="text-amber-400" />
          Vedant Gawali (Age 21)
        </button>
        <button
          type="button"
          onClick={() => applyPreset(30, 100000, 'MODERATE')}
          className={`btn text-xs py-1 px-2.5 flex items-center gap-1.5 ${
            age === 30 && appetite === 'MODERATE' ? 'border-sky-400 bg-sky-500/20 text-sky-200' : ''
          }`}
        >
          <TrendingUp size={13} className="text-sky-400" />
          Salaried Professional (Age 30 · ₹1L/mo)
        </button>
        <button
          type="button"
          onClick={() => applyPreset(60, 40000, 'CONSERVATIVE')}
          className={`btn text-xs py-1 px-2.5 flex items-center gap-1.5 ${
            age === 60 && appetite === 'CONSERVATIVE' ? 'border-emerald-400 bg-emerald-500/20 text-emerald-200' : ''
          }`}
        >
          <ShieldAlert size={13} className="text-emerald-400" />
          Retiree / Senior (Age 60 · Conservative)
        </button>
      </div>

      {/* Interactive Controls */}
      <div className="grid gap-3 sm:grid-cols-3 bg-bg/60 p-3 rounded border border-line">
        <label className="text-xs text-muted">
          User Age ({age} yrs)
          <input
            type="range"
            min={18}
            max={80}
            value={age}
            onChange={(e) => setAge(Number(e.target.value))}
            className="w-full mt-1.5 accent-amber-400"
          />
        </label>
        <label className="text-xs text-muted">
          Monthly Income / Salary (INR)
          <input
            className="input mt-1 w-full num"
            type="number"
            min={0}
            step={1000}
            placeholder="e.g. 45000"
            value={incomeText}
            onChange={(e) => setIncomeText(e.target.value)}
          />
          <div className="mt-1 flex flex-wrap gap-1">
            {[0, 30000, 75000, 150000].map((v) => (
              <button key={v} type="button" className={`rounded border px-1.5 py-0.5 text-[10px] ${income === v ? 'border-amber-400 text-amber-200' : 'border-line text-muted hover:text-text'}`}
                onClick={() => setIncomeText(String(v))}>{v === 0 ? '₹0 (student)' : `₹${v.toLocaleString('en-IN')}`}</button>
            ))}
          </div>
        </label>
        <label className="text-xs text-muted">
          Risk Tolerance Style
          <select
            className="input mt-1 w-full"
            value={appetite}
            onChange={(e) => setAppetite(e.target.value)}
          >
            <option value="AGGRESSIVE">Aggressive (Active Alpha)</option>
            <option value="MODERATE">Moderate (Balanced Intraday)</option>
            <option value="CONSERVATIVE">Conservative (Capital Preservation)</option>
          </select>
        </label>
      </div>

      {/* Recommendations Breakdown */}
      {recs && (
        <div className="space-y-3">
          <div className="text-xs text-zinc-300 bg-amber-500/10 border border-amber-500/20 p-2.5 rounded">
            <b>💡 Financial Rationale:</b> {recs.rationale}
          </div>

          {/* the three limits the problem statement requires (enforced by the Risk Firewall) */}
          <div className="grid gap-3 sm:grid-cols-3 text-xs">
            <div className="p-2.5 rounded border border-line bg-bg">
              <div className="text-muted text-[11px]">Recommended Max Daily Loss</div>
              <div className="text-base font-semibold text-amber-300 num mt-0.5">
                {fmtINR(recs.global_limits.max_daily_loss)}
              </div>
              <div className="text-[10px] text-muted">({recs.global_limits.max_daily_loss_pct}% of capital · account; per strategy below)</div>
            </div>
            <div className="p-2.5 rounded border border-line bg-bg">
              <div className="text-muted text-[11px]">Max Position Size</div>
              <div className="text-base font-semibold text-text num mt-0.5">
                {recs.strategy_limits.max_position} shares
              </div>
              <div className="text-[10px] text-muted">per strategy · larger orders are rejected</div>
            </div>
            <div className="p-2.5 rounded border border-line bg-bg">
              <div className="text-muted text-[11px]">Max Orders per Minute</div>
              <div className="text-base font-semibold text-sky-300 num mt-0.5">
                {recs.strategy_limits.max_orders_per_min} / min
              </div>
              <div className="text-[10px] text-muted">per strategy · stops runaway order loops</div>
            </div>
          </div>

          {/* Strategy Distribution Table */}
          <div>
            <div className="text-xs font-semibold text-muted mb-1.5 uppercase tracking-wider">
              Recommended Strategy Splits on Account
            </div>
            <div className="overflow-x-auto">
              <table className="tbl text-xs">
                <thead>
                  <tr>
                    <th>Strategy</th>
                    <th>Recommended Allocation</th>
                    <th>Max Loss/Day</th>
                    <th>Max Position</th>
                    <th>Orders/Min</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(recs.strategies).map(([sid, st]: any) => (
                    <tr key={sid}>
                      <td className="font-medium text-text">{sid}</td>
                      <td className="num">{fmtINR(st.allocation)}</td>
                      <td className="num text-amber-300">{fmtINR(st.max_daily_loss)}</td>
                      <td className="num">{st.max_position} shares</td>
                      <td className="num">{st.max_orders_per_min} / min</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Admin Apply Controls */}
          {isAdmin && (
            <div className="flex flex-wrap items-center justify-between gap-3 pt-2 border-t border-line">
              <label className="flex items-center gap-2 text-xs text-muted cursor-pointer">
                <input
                  type="checkbox"
                  checked={applyAllocations}
                  onChange={(e) => setApplyAllocations(e.target.checked)}
                  className="rounded accent-amber-400"
                />
                Also update strategy capital allocations automatically
              </label>

              <button
                type="button"
                disabled={applying || loading}
                onClick={handleApply}
                className="btn btn-primary flex items-center gap-1.5"
              >
                {applying ? (
                  <RefreshCw size={14} className="animate-spin" />
                ) : applySuccess ? (
                  <Check size={14} className="text-emerald-400" />
                ) : (
                  <Sparkles size={14} />
                )}
                {applySuccess ? 'Limits Applied Successfully!' : 'Apply Recommended Risk Limits'}
              </button>
            </div>
          )}
        </div>
      )}
    </Panel>
  )
}
