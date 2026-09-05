import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { listApprovals, approveAction, denyAction } from '../lib/api'
import type { PendingApproval } from '../lib/api'

// Task 3.10 — where a person actually decides. Everything up to here only
// ever queues a big action; nothing runs until someone opens this page.
// The manual's own reasoning: no company will let an AI approve a large
// refund unsupervised, so neither does this — approving is an authenticated
// click here, never something the call itself can do.

const card = 'bg-white/4 border border-white/8 rounded-2xl p-5'

export default function ApprovalsPage() {
  const navigate = useNavigate()
  const [approvals, setApprovals] = useState<PendingApproval[]>([])
  const [filter, setFilter] = useState<'pending' | 'all'>('pending')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [deciding, setDeciding] = useState<string | null>(null)

  useEffect(() => { refresh() }, [filter])

  async function refresh() {
    setLoading(true)
    try {
      setApprovals(await listApprovals(filter === 'pending' ? 'pending' : undefined))
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  async function decide(id: string, action: 'approve' | 'deny') {
    const label = action === 'approve' ? 'Approve' : 'Deny'
    if (!confirm(`${label} this action? ${action === 'approve' ? 'It will run immediately.' : 'It will never run.'}`)) return
    setDeciding(id)
    try {
      await (action === 'approve' ? approveAction(id) : denyAction(id))
      await refresh()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setDeciding(null)
    }
  }

  return (
    <div className="min-h-screen bg-[#070711] text-white relative overflow-hidden">
      <div className="absolute top-[-15%] right-[-10%] w-[500px] h-[500px] rounded-full bg-violet-700/15 blur-[130px] pointer-events-none" />

      <header className="relative z-10 border-b border-white/8 px-6 py-4 flex items-center gap-3 backdrop-blur-sm">
        <button onClick={() => navigate('/dashboard')} className="text-slate-500 hover:text-white transition-colors">←</button>
        <h1 className="text-sm font-semibold text-slate-200">Approvals</h1>
      </header>

      <main className="relative z-10 max-w-2xl mx-auto px-6 py-10 space-y-6">
        <p className="text-sm text-slate-400">
          Actions above a value you've set wait here instead of happening on their own. Nothing
          below runs until you say so.
        </p>

        <div className="flex gap-2">
          {(['pending', 'all'] as const).map(f => (
            <button key={f} onClick={() => setFilter(f)}
              className={`text-xs px-3 py-1.5 rounded-lg transition-all ${
                filter === f ? 'bg-violet-600 text-white' : 'bg-white/6 text-slate-400 hover:bg-white/10'
              }`}>
              {f === 'pending' ? 'Awaiting decision' : 'All'}
            </button>
          ))}
        </div>

        {error && (
          <div className="bg-red-500/10 border border-red-500/25 rounded-xl px-4 py-3 text-sm text-red-300">
            {error}
          </div>
        )}

        {loading ? (
          <div className="text-center py-10">
            <div className="w-6 h-6 border-2 border-violet-500 border-t-transparent rounded-full animate-spin mx-auto" />
          </div>
        ) : approvals.length === 0 ? (
          <div className={`${card} text-center text-sm text-slate-500`}>
            {filter === 'pending' ? 'Nothing waiting on you right now.' : 'No approvals yet.'}
          </div>
        ) : (
          <div className="space-y-3">
            {approvals.map(a => (
              <div key={a.id} className={card}>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-sm text-violet-300">{a.tool_name}</span>
                      <StatusPill status={a.status} />
                    </div>
                    <p className="text-sm text-slate-300 mt-1">
                      Amount <span className="font-semibold text-white">{a.amount}</span>
                      <span className="text-slate-500"> — above the {a.threshold} threshold</span>
                    </p>
                    {Object.keys(a.arguments).length > 0 && (
                      <pre className="text-xs font-mono text-slate-500 mt-2 bg-black/30 rounded-lg px-3 py-2 overflow-x-auto">
                        {JSON.stringify(a.arguments, null, 2)}
                      </pre>
                    )}
                    <p className="text-xs text-slate-600 mt-2">
                      Requested {new Date(a.created_at).toLocaleString()}
                      {a.decided_at && ` · decided ${new Date(a.decided_at).toLocaleString()} by ${a.decided_by}`}
                    </p>
                  </div>
                  {a.status === 'pending' && (
                    <div className="flex gap-2 shrink-0">
                      <button onClick={() => decide(a.id, 'approve')} disabled={deciding === a.id}
                        className="text-xs px-3 py-1.5 rounded-lg bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-300 disabled:opacity-50">
                        Approve
                      </button>
                      <button onClick={() => decide(a.id, 'deny')} disabled={deciding === a.id}
                        className="text-xs px-3 py-1.5 rounded-lg bg-red-500/10 hover:bg-red-500/20 text-red-300 disabled:opacity-50">
                        Deny
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}

function StatusPill({ status }: { status: PendingApproval['status'] }) {
  const styles = {
    pending: 'bg-amber-500/15 text-amber-300',
    // Deliberately not green: the action has been released to run but has
    // not reported back. Saying "approved" here would claim something
    // finished that may still be in flight.
    approving: 'bg-sky-500/15 text-sky-300',
    approved: 'bg-emerald-500/15 text-emerald-300',
    denied: 'bg-red-500/15 text-red-300',
  }
  const label = status === 'approving' ? 'running' : status
  return <span className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ${styles[status]}`}>{label}</span>
}
