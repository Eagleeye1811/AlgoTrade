import { useEffect, useState } from 'react'
import { Send } from 'lucide-react'
import { useApi } from '../hooks/useEvents'
import { useAuth } from '../hooks/auth'
import { api } from '../services/api'
import { Badge, ErrorText, Panel } from './ui'

const LABELS: Record<string, [string, string]> = {
  trades: ['Trades', 'one message per order: fill price, strategy, reason, position, P&L'],
  rejections: ['Rejections', 'Risk Firewall / broker rejections with the rule and reason'],
  risk: ['Risk', 'circuit-breaker halt + flatten, 80% of the daily loss limit'],
  system: ['System', 'kill switch; ADMIN: orders blocked, SAFE MODE, component failures'],
  summary: ['Daily summary', 'end-of-day P&L per strategy (weekdays)'],
}

/** Link the user's Telegram chat: explainable alerts + two-tap control from the phone. */
export default function TelegramPanel() {
  const { user } = useAuth()
  const t = useApi<any>('/telegram')
  const [link, setLink] = useState<{ url: string; code: string } | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const d = t.data

  // while a link is pending, poll until the bot reports the chat as linked
  useEffect(() => {
    if (!link || d?.linked) { if (d?.linked) setLink(null); return }
    const id = window.setInterval(() => t.reload(), 2500)
    return () => clearInterval(id)
  }, [link, d?.linked])

  const run = async (fn: () => Promise<any>, ok?: string) => {
    setErr(null); setNote(null)
    try { await fn(); if (ok) setNote(ok); t.reload() } catch (e: any) { setErr(e.message) }
  }
  const connect = () => run(async () => {
    const r = await api.post('/telegram/link')
    setLink(r)
    window.open(r.url, '_blank', 'noopener')
  })

  return (
    <Panel title={<span className="flex items-center gap-2"><Send size={13} /> Telegram copilot</span>}
      right={d && (d.linked ? <Badge tone="green">LINKED{d.muted ? ' · MUTED' : ''}</Badge> : d.configured ? <Badge>NOT LINKED</Badge> : <Badge tone="amber">NOT CONFIGURED</Badge>)}
      bodyClass="p-3 space-y-3 text-xs">
      <p className="text-muted">Get an explained alert for every fill, rejection, halt and kill switch on your phone, and pause or resume
        your strategies in two taps{user?.role === 'ADMIN' ? ' (ADMIN: trigger the kill switch with a confirm button)' : ''}.
        Every action from Telegram is permission-checked and written to the audit trail.</p>
      <ErrorText error={err ?? t.error} />{note && <div className="text-green-400">{note}</div>}
      {!d ? null : !d.configured ? (
        <div className="rounded border border-amber-500/30 bg-amber-500/10 p-2 text-amber-200">
          The server has no bot token. Create a bot with <b>@BotFather</b> (<code>/newbot</code>), put the token in
          <code> backend/.env</code> as <code>ALGOTRADE_TELEGRAM_BOT_TOKEN</code> and restart the backend.
        </div>
      ) : !d.linked ? (
        <div className="space-y-2">
          <button className="btn btn-primary" onClick={connect}><Send size={13} /> Connect Telegram</button>
          {link && (
            <div className="rounded border border-line p-2">
              <div>Telegram should open <b>@{d.bot_username}</b> - press <b>Start</b>. Waiting for the link…</div>
              <div className="mt-1 text-muted">Not opened? <a className="text-info underline" href={link.url} target="_blank" rel="noopener noreferrer">{link.url}</a> (valid 10 minutes, single use)</div>
            </div>
          )}
        </div>
      ) : (
        <div className="space-y-3">
          <div className="grid gap-1.5 sm:grid-cols-2">
            {d.categories.map((c: string) => (
              <label key={c} className="flex cursor-pointer items-start gap-2 rounded border border-line p-2">
                <input type="checkbox" className="mt-0.5" checked={!!d.prefs[c]} disabled={d.muted}
                  onChange={(e) => run(() => api.put('/telegram/prefs', { prefs: { [c]: e.target.checked } }))} />
                <span><span className="font-medium text-text">{LABELS[c]?.[0] ?? c}</span><span className="block text-muted">{LABELS[c]?.[1]}</span></span>
              </label>
            ))}
          </div>
          <div className="flex flex-wrap gap-2">
            <button className="btn" onClick={() => run(() => api.post('/telegram/test'), 'Test alert sent - check Telegram')}>Send test alert</button>
            <button className="btn" onClick={() => run(() => api.put('/telegram/prefs', { muted: !d.muted }))}>{d.muted ? 'Unmute alerts' : 'Mute alerts'}</button>
            <button className="btn btn-danger ml-auto" onClick={() => run(() => api.del('/telegram/link'), 'Telegram disconnected')}>Disconnect</button>
          </div>
          <div className="rounded border border-line p-2 text-muted">
            In the chat: <code>/status</code> <code>/pnl</code> <code>/positions</code> <code>/strategies</code> (Pause/Resume buttons)
            <code> /pause 1</code> <code>/resume 1</code> <code>/summary</code> <code>/mute</code> <code>/unmute</code> <code>/unlink</code>
            {user?.role === 'ADMIN' && <> <code>/kill</code></>} <code>/help</code>
          </div>
        </div>
      )}
    </Panel>
  )
}
