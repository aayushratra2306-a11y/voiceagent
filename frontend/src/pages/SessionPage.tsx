import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { listBots, connectBot, getIceServers, fetchDocumentBlobUrl, sendIceCandidates } from '../lib/api'
import type { Bot } from '../lib/api'

type Status = 'idle' | 'connecting' | 'connected' | 'error'

// Task 2.10 — one citation, as published by the backend's RAG processor
// over the data channel ({"type": "rag-sources", "sources": [...]}).
interface RagSource {
  doc_id: string | null
  filename: string
  page: number | null
  // null on the reranker-failure fallback path: raw cosine scores are on a
  // different scale, so the backend sends nothing rather than a number that
  // would read as a confidence value and be wrong.
  score: number | null
  has_file: boolean
}

export default function SessionPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [bot, setBot] = useState<Bot | null>(null)
  const [status, setStatus] = useState<Status>('idle')
  const [log, setLog] = useState<string[]>([])
  const [speaking, setSpeaking] = useState(false)
  // null = no answer yet this session. [] = the bot answered, but from
  // general knowledge rather than a document. Those are different states
  // and the UI says so — an empty list is a real result, not "no data".
  const [sources, setSources] = useState<RagSource[] | null>(null)
  const [openingDoc, setOpeningDoc] = useState<string | null>(null)
  const pcRef = useRef<RTCPeerConnection | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const dcRef = useRef<RTCDataChannel | null>(null)
  // Set the instant a start begins, cleared only by stopSession(). See the
  // comment at the top of startSession() for why `status` cannot do this.
  const startingRef = useRef(false)
  // Blob URLs stay alive while their tab is open — revoking one immediately
  // after window.open() gives the user a blank viewer. Held here and
  // released together when the session ends.
  const blobUrlsRef = useRef<string[]>([])

  useEffect(() => {
    listBots().then(bots => setBot(bots.find(b => b.id === id) ?? null))
    audioRef.current = new Audio()
    audioRef.current.autoplay = true
    return () => stopSession()
  }, [])

  function addLog(msg: string) {
    setLog(prev => [...prev.slice(-50), msg])
  }

  async function startSession() {
    // Re-entrancy guard. `status` cannot do this job: setStatus is async, so
    // two clicks landing in the same React batch both read status==='idle'
    // and both proceed. The second run then overwrites pcRef.current, which
    // strands the FIRST RTCPeerConnection — still live, still sending the
    // mic, but no longer referenced anywhere the page can reach, so
    // stopSession() cannot close it and the user cannot see it exists.
    // Root-caused 2026-09-03 from a call where exactly that happened: the
    // server ran two pipelines for one caller and the two bots talked over
    // each other. A ref is checked and set synchronously, so it actually
    // closes the window that `status` leaves open.
    if (startingRef.current) return
    startingRef.current = true
    // Belt and braces — never build a second connection on top of a live
    // one, whatever route got us here. closeConnection() rather than
    // stopSession() on purpose: stopSession clears the guard we just set,
    // which would hand the very race above back to the second click.
    closeConnection()
    setStatus('connecting')
    setLog([])
    setSources(null)
    addLog('Requesting microphone…')
    try {
      // Explicit audio constraints — without these, some browsers/setups
      // don't reliably apply echo cancellation, so the bot's own voice from
      // the speakers can bleed back into the mic and get picked up as the
      // user interrupting it. Root-caused 2026-08-30 via backend logs
      // showing the bot's own replies getting cut short mid-sentence,
      // correlated with interruption events — classic echo, not a VAD
      // tuning issue. echoCancellation is the fix; the other two are
      // standard companions for voice-agent audio quality.
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      })
      streamRef.current = stream

      // Task 2.3 — ask the server what ICE servers to use rather than
      // hardcoding STUN here. STUN alone only tells each side its own public
      // address; it cannot help when neither side is directly reachable,
      // which is exactly the case on symmetric NAT and many mobile carriers.
      // The TURN relay that handles those lives in the backend's config, so
      // the browser has to be told about it.
      //
      // Falls back to public STUN if the lookup fails: that still connects
      // on ordinary networks, which beats not starting the call at all.
      let iceServers: RTCIceServer[] = [
        { urls: 'stun:stun.l.google.com:19302' },
        { urls: 'stun:stun1.l.google.com:19302' },
      ]
      try {
        iceServers = await getIceServers()
        const hasTurn = iceServers.some(s =>
          (Array.isArray(s.urls) ? s.urls : [s.urls]).some(u => u.startsWith('turn:')),
        )
        addLog(`ICE config: ${iceServers.length} server(s)${hasTurn ? ', TURN relay available' : ', STUN only'}`)
      } catch {
        addLog('ICE config lookup failed — falling back to public STUN')
      }

      const pc = new RTCPeerConnection({ iceServers })
      pcRef.current = pc

      stream.getTracks().forEach(t => pc.addTrack(t, stream))

      pc.ontrack = e => {
        if (audioRef.current) audioRef.current.srcObject = e.streams[0]
        addLog('Bot audio connected ✓')
      }

      // Trickle ICE. Candidates are discovered over several seconds; the ones
      // found before the server replies with a pc_id have nowhere to go yet,
      // so they are buffered here and flushed the moment it arrives.
      let pcId: string | null = null
      const pending: RTCIceCandidate[] = []

      pc.onicecandidate = e => {
        if (!e.candidate) { addLog('ICE gathering complete'); return }
        addLog(`Candidate: ${e.candidate.type} ${e.candidate.address ?? ''}`)
        if (pcId) sendIceCandidates(pcId, [e.candidate])
        else pending.push(e.candidate)
      }

      pc.oniceconnectionstatechange = () => {
        addLog(`ICE: ${pc.iceConnectionState}`)
        if (pc.iceConnectionState === 'connected' || pc.iceConnectionState === 'completed') {
          setStatus('connected')
          addLog('Ready — speak now')
        }
        if (pc.iceConnectionState === 'failed' || pc.iceConnectionState === 'disconnected') {
          setStatus('error')
          addLog('Connection lost')
        }
      }

      const ctx = new AudioContext()
      const analyser = ctx.createAnalyser()
      const src = ctx.createMediaStreamSource(stream)
      src.connect(analyser)
      const data = new Uint8Array(analyser.frequencyBinCount)
      const tick = () => {
        if (!pcRef.current) return
        analyser.getByteFrequencyData(data)
        const vol = data.reduce((a, b) => a + b, 0) / data.length
        setSpeaking(vol > 10)
        requestAnimationFrame(tick)
      }
      tick()

      // Task 2.10 — the browser MUST create this channel, and must do it
      // before createOffer() so it lands in the SDP. Pipecat's server side
      // only *listens* for a channel (connection.py:330); it never opens
      // one. If none arrives within DATA_CHANNEL_TIMEOUT_SECS (10s) the
      // server permanently sets _data_channel_enabled = False and silently
      // drops every message from then on — which is exactly the
      // "Data channel not ready, queuing message" line in the live logs.
      // The label is arbitrary: the server accepts whatever the client makes.
      const dc = pc.createDataChannel('pipecat')
      dcRef.current = dc

      dc.onopen = () => addLog('Data channel open ✓')

      dc.onmessage = e => {
        try {
          const msg = JSON.parse(e.data)
          if (msg.type === 'rag-sources') {
            setSources(msg.sources as RagSource[])
          }
          // Any other message type is pipecat's business, not ours.
        } catch {
          // Non-JSON traffic (keepalives and the like) is expected — a
          // parse failure here must never interrupt a live call.
        }
      }

      addLog('Creating WebRTC offer…')
      await pc.setLocalDescription(await pc.createOffer())

      // Sent immediately, without waiting for ICE gathering to complete.
      // That wait used to cost up to 5 seconds of dead air at the start of
      // every call, and was longest precisely when a TURN relay is in play,
      // since allocating the relay is its own network round trip. The
      // remaining candidates now follow over /connect/ice while the worker
      // is already starting up, so the two overlap instead of queueing.
      addLog('Connecting to bot…')
      const answer = await connectBot(id!, pc.localDescription!.sdp, pc.localDescription!.type)
      await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type as RTCSdpType })

      pcId = answer.pc_id
      if (pending.length) {
        sendIceCandidates(pcId, pending.splice(0))
      }
      addLog('Handshake complete ✓')
    } catch (e: any) {
      addLog(`Error: ${e.message}`)
      setStatus('error')
      stopSession()
    }
  }

  function stopSession() {
    // Every route out of a live/connecting session comes through here — the
    // Stop button, the error path, and unmount — so this is the one place
    // the start guard is released. While connected it deliberately stays
    // set, which also blocks a second Start on top of a live call.
    startingRef.current = false
    closeConnection()
    setStatus('idle')
    setSpeaking(false)
  }

  /** Pure teardown of whatever is currently open. No status, no guard. */
  function closeConnection() {
    if (dcRef.current) {
      dcRef.current.close()
      dcRef.current = null
    }
    // Released only now, not at click time — see blobUrlsRef.
    blobUrlsRef.current.forEach(u => URL.revokeObjectURL(u))
    blobUrlsRef.current = []
    if (pcRef.current) {
      pcRef.current.close()
      pcRef.current = null
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach(t => t.stop())
      streamRef.current = null
    }
    if (audioRef.current) audioRef.current.srcObject = null
  }

  // Task 2.10 — open a cited document at the page the answer came from.
  // The #page= fragment is honoured by Chrome's built-in PDF viewer and by
  // Firefox's pdf.js; if a browser ignores it the document still opens at
  // page 1, which is a graceful degradation rather than a broken link.
  async function openSource(src: RagSource) {
    if (!src.doc_id || !src.has_file) return
    setOpeningDoc(src.doc_id)
    try {
      const url = await fetchDocumentBlobUrl(src.doc_id)
      blobUrlsRef.current.push(url)
      window.open(src.page ? `${url}#page=${src.page}` : url, '_blank')
    } catch (e: any) {
      addLog(`Could not open source: ${e.message}`)
    } finally {
      setOpeningDoc(null)
    }
  }

  const isActive = status === 'connected'
  const isConnecting = status === 'connecting'

  return (
    <div className="min-h-screen bg-[#070711] text-white flex flex-col relative overflow-hidden">
      {/* Ambient glow that reacts to speaking */}
      <div className={`absolute inset-0 transition-all duration-700 pointer-events-none ${
        isActive && speaking
          ? 'bg-emerald-500/5'
          : isActive
          ? 'bg-violet-500/3'
          : 'bg-transparent'
      }`} />
      <div className={`absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full blur-[100px] pointer-events-none transition-all duration-700 ${
        isActive && speaking
          ? 'w-[600px] h-[600px] bg-emerald-500/12'
          : isActive
          ? 'w-[500px] h-[500px] bg-violet-500/10'
          : 'w-[300px] h-[300px] bg-slate-700/10'
      }`} />

      {/* Header */}
      <header className="relative z-10 border-b border-white/8 px-6 py-4 flex items-center gap-3 backdrop-blur-sm">
        <button
          onClick={() => { stopSession(); navigate('/dashboard') }}
          className="p-1.5 text-slate-500 hover:text-white hover:bg-white/8 rounded-lg transition-all"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>
          </svg>
        </button>

        <div className="flex items-center gap-2.5">
          <p className="font-semibold text-white">{bot?.name ?? '…'}</p>
          <span className={`inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full font-medium ${
            status === 'connected' ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/20'
            : status === 'connecting' ? 'bg-amber-500/15 text-amber-400 border border-amber-500/20'
            : status === 'error' ? 'bg-red-500/15 text-red-400 border border-red-500/20'
            : 'bg-white/8 text-slate-400 border border-white/10'
          }`}>
            <span className={`w-1.5 h-1.5 rounded-full ${
              status === 'connected' ? 'bg-emerald-400 animate-pulse'
              : status === 'connecting' ? 'bg-amber-400 animate-pulse'
              : status === 'error' ? 'bg-red-400'
              : 'bg-slate-500'
            }`} />
            {status === 'connected' ? 'Live' : status === 'connecting' ? 'Connecting…' : status === 'error' ? 'Error' : 'Ready'}
          </span>
        </div>
      </header>

      {/* Main */}
      <main className="relative z-10 flex-1 flex flex-col items-center justify-center gap-10 p-6">

        {/* Orb */}
        <div className="relative flex items-center justify-center select-none">
          {/* Outer ring — speaking pulse */}
          {isActive && speaking && (
            <>
              <div className="absolute w-64 h-64 rounded-full border border-emerald-500/20 animate-ping" style={{ animationDuration: '1.5s' }} />
              <div className="absolute w-52 h-52 rounded-full border border-emerald-500/30 animate-ping" style={{ animationDuration: '1s' }} />
            </>
          )}
          {isConnecting && (
            <div className="absolute w-52 h-52 rounded-full border-2 border-violet-500/30 border-t-violet-500 animate-spin" />
          )}

          {/* Orb body */}
          <div className={`relative w-40 h-40 rounded-full flex items-center justify-center transition-all duration-500 ${
            isActive && speaking
              ? 'scale-110'
              : isActive
              ? 'scale-100'
              : 'scale-95'
          }`}>
            {/* Gradient background */}
            <div className={`absolute inset-0 rounded-full transition-all duration-500 ${
              isActive && speaking
                ? 'bg-gradient-to-br from-emerald-400 to-teal-600 shadow-[0_0_60px_rgba(52,211,153,0.4)]'
                : isActive
                ? 'bg-gradient-to-br from-violet-500 to-indigo-700 shadow-[0_0_60px_rgba(139,92,246,0.3)]'
                : isConnecting
                ? 'bg-gradient-to-br from-amber-500/60 to-orange-700/60'
                : 'bg-gradient-to-br from-slate-700 to-slate-800'
            }`} />

            {/* Mic icon */}
            <div className="relative z-10">
              {isActive ? (
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
                  <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                  <line x1="12" y1="19" x2="12" y2="23"/>
                  <line x1="8" y1="23" x2="16" y2="23"/>
                </svg>
              ) : (
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,0.4)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="1" y1="1" x2="23" y2="23"/>
                  <path d="M9 9v3a3 3 0 0 0 5.12 2.12M15 9.34V4a3 3 0 0 0-5.94-.6"/>
                  <path d="M17 16.95A7 7 0 0 1 5 12v-2m14 0v2a7 7 0 0 1-.11 1.23"/>
                  <line x1="12" y1="19" x2="12" y2="23"/>
                  <line x1="8" y1="23" x2="16" y2="23"/>
                </svg>
              )}
            </div>

            {/* Speaking wave bars */}
            {isActive && speaking && (
              <div className="absolute -bottom-8 flex items-end gap-1 justify-center">
                {[3, 5, 8, 5, 3].map((h, i) => (
                  <div
                    key={i}
                    className="w-1 bg-emerald-400 rounded-full animate-bounce"
                    style={{ height: `${h * 2}px`, animationDelay: `${i * 0.1}s`, animationDuration: '0.6s' }}
                  />
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Status text */}
        <div className="text-center mt-2">
          <p className="text-slate-300 font-medium">
            {status === 'idle' && 'Press Start to begin'}
            {status === 'connecting' && 'Setting up connection…'}
            {status === 'connected' && speaking && `${bot?.name ?? 'Bot'} is listening…`}
            {status === 'connected' && !speaking && 'Speak naturally — the bot will reply'}
            {status === 'error' && 'Something went wrong'}
          </p>
          {isActive && (
            <p className="text-xs text-slate-600 mt-1">Pause naturally to let the bot respond</p>
          )}
        </div>

        {/* Controls */}
        <div className="flex gap-3">
          {status === 'idle' || status === 'error' ? (
            <button
              onClick={startSession}
              className="flex items-center gap-2.5 bg-gradient-to-r from-violet-600 to-indigo-600 hover:from-violet-500 hover:to-indigo-500 text-white font-semibold px-8 py-3 rounded-2xl transition-all shadow-lg shadow-violet-900/40 text-sm"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
              </svg>
              Start Session
            </button>
          ) : (
            <button
              onClick={stopSession}
              className="flex items-center gap-2.5 bg-red-600/80 hover:bg-red-500 text-white font-semibold px-8 py-3 rounded-2xl transition-all shadow-lg shadow-red-900/30 text-sm"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <rect x="3" y="3" width="18" height="18" rx="2"/>
              </svg>
              End Session
            </button>
          )}
        </div>

        {/* Sources — Task 2.10 */}
        {sources !== null && (
          <div className="w-full max-w-sm bg-white/3 border border-white/8 rounded-2xl p-4">
            <p className="text-[11px] uppercase tracking-wider text-slate-500 font-semibold mb-3">
              {sources.length > 0 ? 'Answered from' : 'Source'}
            </p>

            {sources.length === 0 ? (
              /* An empty list is a real answer, not a missing one: the bot
                 replied from the model's own knowledge rather than from the
                 customer's documents. Saying so plainly is the point — it's
                 the difference between an answer they can verify and one
                 they can't. */
              <div className="flex items-start gap-2.5">
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     strokeWidth="2" strokeLinecap="round" className="text-amber-400/70 mt-0.5 shrink-0">
                  <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>
                  <line x1="12" y1="16" x2="12.01" y2="16"/>
                </svg>
                <p className="text-xs text-slate-400 leading-relaxed">
                  General knowledge — not from your documents.
                </p>
              </div>
            ) : (
              <div className="space-y-2">
                {sources.map((src, i) => {
                  const clickable = src.has_file && !!src.doc_id
                  const busy = openingDoc === src.doc_id
                  return (
                    <button
                      key={`${src.doc_id}-${src.page}-${i}`}
                      onClick={() => openSource(src)}
                      disabled={!clickable || busy}
                      title={clickable
                        ? `Open ${src.filename}${src.page ? ` at page ${src.page}` : ''}`
                        : 'The original file was not stored for this document — re-upload it to enable opening'}
                      className={`w-full flex items-center gap-2.5 text-left px-2.5 py-2 rounded-xl border transition-all ${
                        clickable
                          ? 'border-white/8 hover:border-violet-500/40 hover:bg-violet-500/8 cursor-pointer'
                          : 'border-white/5 cursor-default'
                      }`}
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                           strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                           className="text-slate-500 shrink-0">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                        <polyline points="14 2 14 8 20 8"/>
                      </svg>

                      <span className="min-w-0 flex-1">
                        <span className="block text-xs text-slate-300 truncate">{src.filename}</span>
                        {src.page !== null && (
                          <span className="block text-[11px] text-slate-500">Page {src.page}</span>
                        )}
                      </span>

                      {/* Only shown when the reranker actually produced a
                          score. On the fallback path the backend sends null
                          rather than a raw cosine value, which sits on a
                          different scale and would misrepresent confidence. */}
                      {src.score !== null && (
                        <span className="text-[10px] text-slate-600 tabular-nums shrink-0">
                          {Math.round(src.score * 100)}%
                        </span>
                      )}

                      {busy && (
                        <span className="w-3 h-3 rounded-full border border-violet-400/40 border-t-violet-400 animate-spin shrink-0" />
                      )}
                    </button>
                  )
                })}
              </div>
            )}
          </div>
        )}

        {/* Log */}
        {log.length > 0 && (
          <div className="w-full max-w-sm bg-white/3 border border-white/8 rounded-2xl p-4 font-mono text-xs text-slate-500 space-y-1.5 max-h-36 overflow-y-auto">
            {log.map((l, i) => (
              <div key={i} className={l.includes('✓') ? 'text-emerald-500' : l.includes('Error') || l.includes('lost') ? 'text-red-400' : ''}>
                {l}
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}
