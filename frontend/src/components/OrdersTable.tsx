import { fmtNum, fmtQty, fmtTime, shortId } from '../services/format'
import { Empty, Side, StatusBadge } from './ui'

export default function OrdersTable({ orders, onSelect, compact }: { orders: any[]; onSelect?: (o: any) => void; compact?: boolean }) {
  if (!orders.length) return <Empty>No orders yet</Empty>
  return (
    <div className="overflow-x-auto">
      <table className="tbl">
        <thead><tr>
          <th>Time</th><th>Order</th><th>Strategy</th><th>Symbol</th><th>Side</th><th className="text-right">Req</th><th className="text-right">Filled</th>
          <th className="text-right">Price</th><th>Status</th>{!compact && <><th className="text-right">Latency</th><th>Origin</th><th>Reason</th></>}
        </tr></thead>
        <tbody>
          {orders.map((o) => (
            <tr key={o.id} className={onSelect ? 'clickable' : ''} onClick={() => onSelect?.(o)}>
              <td className="num text-muted">{fmtTime(o.created_at)}</td>
              <td className="num">{shortId(o.id)}</td>
              <td>{o.strategy_id}</td><td>{o.symbol}</td><td><Side side={o.side} /></td>
              <td className="num text-right">{fmtQty(o.quantity)}</td>
              <td className="num text-right">{fmtQty(o.filled_quantity)}</td>
              <td className="num text-right">{o.avg_fill_price != null ? fmtNum(o.avg_fill_price) : o.limit_price != null ? `L ${fmtNum(o.limit_price)}` : '—'}</td>
              <td><StatusBadge status={o.status} /></td>
              {!compact && <>
                <td className="num text-right">{o.latency_ms != null ? `${o.latency_ms} ms` : '—'}</td>
                <td className="text-muted">{o.origin}{o.reduce_only ? ' · close' : ''}</td>
                <td className="max-w-[280px] truncate text-muted" title={o.reject_reason || ''}>{o.reject_reason || ''}</td>
              </>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
