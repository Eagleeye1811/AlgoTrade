import { StatusBadge } from './ui'

export default function RiskChecks({ checks }: { checks: any[] }) {
  return (
    <div className="overflow-x-auto"><table className="tbl">
      <thead><tr><th>Rule</th><th>Check</th><th>Result</th><th>Actual</th><th>Allowed</th><th>Message</th></tr></thead>
      <tbody>{checks.map((c, i) => (
        <tr key={i} className={c.result === 'FAIL' ? 'bg-red-500/5' : ''}>
          <td className="font-mono text-[11px]">{c.rule}</td><td>{c.label}</td><td><StatusBadge status={c.result} /></td>
          <td className="num">{c.actual || '—'}</td><td className="num">{c.allowed || '—'}</td><td className="whitespace-normal text-muted">{c.message}</td>
        </tr>))}</tbody>
    </table></div>
  )
}
