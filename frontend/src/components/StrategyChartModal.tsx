import { useEffect } from 'react'
import { Link } from 'react-router-dom'
import { X } from 'lucide-react'
import { useApi } from '../hooks/useEvents'
import StrategyChart from '../charts/StrategyChart'
import { Loading, StatusBadge } from './ui'

/** A strategy's own chart (its indicators, levels and fills) in a large window, opened from anywhere. */
export default function StrategyChartModal({ strategy, onClose }: { strategy: any | null; onClose: () => void }) {
  const chart = useApi<any>(strategy ? `/strategies/${strategy.id}/chart?limit=300` : null, ['strategy.config'], 1000)
  useEffect(() => {
    if (!strategy) return
    const h = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [strategy, onClose])
  if (!strategy) return null
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4" onMouseDown={onClose}>
      <div className="panel flex max-h-[94vh] w-full max-w-6xl flex-col overflow-auto shadow-2xl" onMouseDown={(e) => e.stopPropagation()}>
        <div className="panel-h">
          <span className="flex items-center gap-2 normal-case tracking-normal text-text">
            <b>{strategy.name}</b> <span className="text-muted">· {strategy.symbol} · {strategy.timeframe} · {strategy.id}</span> <StatusBadge status={strategy.status} />
          </span>
          <span className="flex items-center gap-2">
            <Link className="btn py-1" to={`/strategies/${strategy.id}`} onClick={onClose}>Open details</Link>
            <button onClick={onClose} className="text-muted hover:text-text" title="Close (Esc)"><X size={16} /></button>
          </span>
        </div>
        {chart.data && chart.data.strategy_id === strategy.id ? <StrategyChart data={chart.data} height={520} /> : <div className="p-6"><Loading /></div>}
      </div>
    </div>
  )
}
