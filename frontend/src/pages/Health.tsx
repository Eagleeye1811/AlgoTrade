import { Fragment, useState } from 'react'
import { useApi, useEvents } from '../hooks/useEvents'
import { api } from '../services/api'
import { fmtINR, fmtNum, fmtQty, fmtTime } from '../services/format'
import { Badge, Empty, ErrorText, Loading, Panel, Side, StatusBadge } from '../components/ui'

export default function Health() {
  const health = useApi<any>('/health', ['reconciliation.completed', 'trading.status'], 2000)
  useEvents(['health.update'], (e) => health.setData((d: any) => (d ? { ...d, components: e.payload.components, overall: e.payload.overall } : d)))
  const recon = useApi<any>('/reconciliation?limit=30', ['reconciliation.completed', 'reconciliation.started'], 500)
  const broker = useApi<any>('/broker/state', ['reconciliation.completed', 'fill.received', 'fault.step'], 2000)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [open, setOpen] = useState<number | null>(null)
  const run = async () => { setBusy(true); setErr(null); try { await api.post('/reconciliation/run'); recon.reload(); broker.reload() } catch (e: any) { setErr(e.message) } finally { setBusy(false) } }
  const [adopted, setAdopted] = useState<string | null>(null)
  const adopt = async () => {
    setBusy(true); setErr(null); setAdopted(null)
    try {
      const r: any = await api.post('/reconciliation/adopt-baseline')
      setAdopted(`Acknowledged ${r.acknowledged_fills} fill(s) and ${r.acknowledged_orders} order(s) at the broker as external · reconciliation ${r.reconciliation.status}`)
      recon.reload(); broker.reload()
    } catch (e: any) { setErr(e.message) } finally { setBusy(false) }
  }
  const h = health.data
  const unattributed = recon.data?.status === 'MISMATCH_UNRESOLVED' && (recon.data.runs.find((r: any) => r.status !== 'RUNNING')?.details?.unresolved ?? [])
    .every((u: any) => u.type === 'MISSING_FILL' || u.type === 'ORPHAN_BROKER_ORDER')
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3"><h1 className="text-base font-semibold">Reconciliation &amp; System Health</h1>{h && <StatusBadge status={h.overall} />}
        <button className="btn btn-primary ml-auto" disabled={busy} onClick={run}>{busy ? 'Reconciling…' : 'Run reconciliation'}</button></div>
      <ErrorText error={err} />
      {unattributed && (
        <div className="flex flex-wrap items-center gap-3 rounded border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
          <span>The broker has fills/orders AlgoTrade has no record of (e.g. after a database reset). If positions match, an ADMIN can
            acknowledge them as external - audited; anything new still blocks.</span>
          <button className="btn ml-auto" disabled={busy} onClick={adopt}>Acknowledge external broker trades</button>
        </div>
      )}
      {adopted && <div className="rounded border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300">{adopted}</div>}
      {!h ? <Loading /> : (
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
          {h.components.map((c: any) => (
            <div key={c.key} className={`panel p-3 text-xs ${c.status === 'FAILED' ? 'border-red-500/50' : c.status === 'WARNING' ? 'border-amber-500/40' : ''}`}>
              <div className="flex items-center justify-between"><span className="font-semibold text-white">{c.name}</span><StatusBadge status={c.status} /></div>
              <div className="mt-1 min-h-[2.2em] text-muted">{c.detail}</div>
              <div className="mt-1 grid grid-cols-2 gap-x-2 text-[11px]">
                <span className="text-muted">Latency</span><span className="num text-right">{c.latency_ms != null ? `${fmtNum(c.latency_ms, 1)} ms` : '—'}</span>
                <span className="text-muted">Heartbeat</span><span className="num text-right">{fmtTime(c.last_heartbeat)}</span>
                <span className="text-muted">Last success</span><span className="num text-right">{fmtTime(c.last_success)}</span>
              </div>
              {c.recent_errors.length > 0 && <div className="mt-1 space-y-0.5 text-[11px] text-red-300">{c.recent_errors.slice(0, 3).map((e: any, i: number) => <div key={i}>{fmtTime(e.at)} {e.message}</div>)}</div>}
            </div>
          ))}
        </div>
      )}
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title={`Last recovery${h?.recovery ? ` · ${h.recovery.trigger} · ${h.recovery.duration_ms} ms` : ''}`} bodyClass="p-3 space-y-1.5 text-xs">
          {!h?.recovery ? <Empty>No recovery recorded</Empty> : h.recovery.steps.map((s: any, i: number) => (
            <div key={i} className="flex gap-2"><StatusBadge status={s.status} /><div><div className="font-medium">{s.title}</div><div className="text-muted">{s.detail}</div></div></div>
          ))}
        </Panel>
        <Panel title="Platform vs broker positions" right={<button className="btn" onClick={broker.reload}>Refresh</button>} bodyClass="p-3 text-xs space-y-2">
          {broker.error ? <ErrorText error={broker.error} /> : !broker.data ? <Loading /> : <BrokerCompare b={broker.data} />}
        </Panel>
      </div>
      <Panel title={`Reconciliation runs · current: ${recon.data?.status ?? '—'}`}>
        {!recon.data ? <Loading /> : recon.data.runs.length === 0 ? <Empty>No reconciliation runs yet</Empty> : (
          <div className="overflow-x-auto"><table className="tbl"><thead><tr><th>Started</th><th>Trigger</th><th>Status</th><th>Mismatches</th><th>Actions</th><th>Summary</th></tr></thead>
            <tbody>{recon.data.runs.map((r: any) => (
              <Fragment key={r.id}>
                <tr className="clickable" onClick={() => setOpen(open === r.id ? null : r.id)}>
                  <td className="num text-muted">{fmtTime(r.started_at, true)}</td><td>{r.trigger}</td><td><StatusBadge status={r.status} /></td>
                  <td className="num">{r.mismatches.length}</td><td className="num">{r.actions.length}</td><td className="max-w-[460px] truncate">{r.summary}</td>
                </tr>
                {open === r.id && <tr><td colSpan={6} className="bg-bg p-3 whitespace-normal"><ReconDetail r={r} /></td></tr>}
              </Fragment>))}</tbody></table></div>
        )}
      </Panel>
    </div>
  )
}

function ReconDetail({ r }: { r: any }) {
  const d = r.details || {}
  return (
    <div className="space-y-3 text-xs">
      {d.comparison?.length > 0 && (
        <table className="tbl"><thead><tr><th>Symbol</th><th className="text-right">Internal (before)</th><th className="text-right">Broker</th><th className="text-right">Internal (after)</th><th>Result</th></tr></thead>
          <tbody>{d.comparison.map((c: any) => (
            <tr key={c.symbol}><td>{c.symbol}</td><td className="num text-right">{fmtQty(c.internal_before)}</td><td className="num text-right">{fmtQty(c.broker)}</td><td className="num text-right">{fmtQty(c.internal_after)}</td>
              <td>{c.status === 'MATCH' ? <Badge tone="green">match</Badge> : c.status === 'RESOLVED' ? <Badge tone="amber">mismatch detected → resolved</Badge> : <Badge tone="red">MISMATCH DETECTED</Badge>}</td></tr>))}</tbody></table>
      )}
      {d.account && (d.account.funds_api === false
        ? <div className="text-muted">{d.account.note}</div>
        : <div className="num">Broker cash {fmtINR(d.account.broker_cash, 2)} · internal ledger {fmtINR(d.account.internal_expected_cash, 2)} · difference {fmtINR(d.account.difference, 2)}</div>)}
      {r.mismatches.length > 0 && <div><div className="text-muted">Mismatches</div>{r.mismatches.map((m: any, i: number) => <div key={i}><Badge tone="amber">{m.type}</Badge> {m.detail}</div>)}</div>}
      {r.actions.length > 0 && <div><div className="text-muted">Repair actions (from broker executions)</div>{r.actions.map((a: any, i: number) => <div key={i}><Badge tone="green">{a.action}</Badge> {a.detail}</div>)}</div>}
      {d.unresolved?.length > 0 && <div><div className="text-red-300">Unresolved — new orders blocked</div>{d.unresolved.map((u: any, i: number) => <div key={i}><Badge tone="red">{u.type}</Badge> {u.detail}</div>)}</div>}
    </div>
  )
}

function BrokerCompare({ b }: { b: any }) {
  const syms = Array.from(new Set([...Object.keys(b.internal_net || {}), ...b.positions.map((p: any) => p.symbol)]))
  const bmap = Object.fromEntries(b.positions.map((p: any) => [p.symbol, p]))
  const active = Object.entries(b.faults).filter(([, v]) => v && v !== 0)
  return (
    <>
      <div className="overflow-x-auto"><table className="tbl w-full">
        <thead><tr><th>Stock</th><th className="text-right" title="sum of all strategies' positions">Platform</th><th className="text-right">Broker</th>
          <th className="text-right">Broker avg price</th><th className="text-right">Status</th></tr></thead>
        <tbody>{syms.length === 0 ? <tr><td colSpan={5} className="text-muted">No positions on either side</td></tr> : syms.map((s) => {
          const i = Number(b.internal_net[s] ?? 0), br = Number(bmap[s]?.quantity ?? 0)
          return <tr key={s}><td className="font-medium">{s}</td>
            <td className="num text-right whitespace-nowrap"><Side side={i > 0 ? 'LONG' : i < 0 ? 'SHORT' : 'FLAT'} /> {fmtQty(i)}</td>
            <td className="num text-right">{fmtQty(br)}</td><td className="num text-right">{bmap[s] ? fmtNum(bmap[s].avg_price) : '—'}</td>
            <td className="text-right">{i === br ? <Badge tone="green">match</Badge> : <Badge tone="red">mismatch</Badge>}</td></tr>
        })}</tbody></table></div>
      <div className="text-muted">Broker account <span className="font-medium text-text">{b.account.account_id}</span>{b.account.extra?.funds_api === false
        ? <> · funds not exposed by the 021 API{b.account.extra?.token_expires_at ? <> · session valid until <span className="num text-text">{new Date(b.account.extra.token_expires_at).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })}</span></> : null}</>
        : <>: cash {fmtINR(b.account.cash, 2)} · margin used {fmtINR(b.account.margin_used)} · starting {fmtINR(b.account.starting_cash)}</>}</div>
      <div className="flex flex-wrap items-center gap-2"><span className="text-muted">Injected broker faults:</span>
        {active.length === 0 ? <Badge tone="green">none</Badge> : active.map(([k, v]) => <Badge key={k} tone="amber">{k}: {String(v)}</Badge>)}</div>
    </>
  )
}
