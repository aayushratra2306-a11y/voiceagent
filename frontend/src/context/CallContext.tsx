import { createContext, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { connectBot, getIceServers, fetchDocumentBlobUrl, sendIceCandidates } from '../lib/api'
import type { Bot } from '../lib/api'

export type CallStatus = 'idle' | 'connecting' | 'connected' | 'error'

// Task 2.10 — one citation, as published by the backend's RAG processor
// over the data channel ({"type": "rag-sources", "sources": [...]}).
export interface RagSource {
  doc_id: string | null
  filename: string
  page: number | null
  // null on the reranker-failure fallback path: raw cosine scores are on a
  // different scale, so the backend sends nothing rather than a number that
  // would read as a confidence value and be wrong.
  score: number | null
  has_file: boolean
}

interface CallContextType {
  /** The bot on the call. Non-null from the moment a call starts until it ends. */
  bot: Bot | null
  status: CallStatus
  /** True while the caller's own mic is above the speaking threshold. */
  speaking: boolean
  muted: boolean
  log: string[]
  sources: RagSource[] | null
  /** epoch ms the media actually connected — null until then. Drives the timer. */
  connectedAt: number | null
  openingDoc: string | null
  startCall: (bot: Bot) => Promise<void>
  endCall: () => void
  toggleMute: () => void
  openSource: (src: RagSource) => Promise<void>
}

const CallContext = createContext<CallContextType | null>(null)

/**
 * Owns the live call for the whole application.
 *
 * This provider is mounted ABOVE <Routes>, and that placement is the entire
 * point of it. The peer connection, the microphone stream and the audio
 * element used to live in SessionPage, whose unmount cleanup ended the call
 * — so navigating anywhere hung up on the caller. The one workflow the
 * product is built around (task 3.10: a person approves a large refund while
 * the caller waits on the line) was therefore impossible without opening a
 * second browser tab.
 *
 * Nothing here is tied to a route. A call ends when someone ends it.
 */
export function CallProvider({ children }: { children: ReactNode }) {
  const [bot, setBot] = useState<Bot | null>(null)
  const [status, setStatus] = useState<CallStatus>('idle')
  const [speaking, setSpeaking] = useState(false)
  const [muted, setMuted] = useState(false)
  const [log, setLog] = useState<string[]>([])
  // null = no answer yet this session. [] = the bot answered, but from
  // general knowledge rather than a document. Those are different states
  // and the UI says so — an empty list is a real result, not "no data".
  const [sources, setSources] = useState<RagSource[] | null>(null)
  const [connectedAt, setConnectedAt] = useState<number | null>(null)
  const [openingDoc, setOpeningDoc] = useState<string | null>(null)

  const pcRef = useRef<RTCPeerConnection | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const dcRef = useRef<RTCDataChannel | null>(null)
  // The Web Audio graph driving the speaking indicator. Held so it can be
  // torn down: a browser allows only a handful of live AudioContexts (six in
  // Chrome), and before this refactor each call ran in a freshly mounted
  // page, so leaking one per call was invisible. Now the app is long-lived
  // and someone can start ten calls without a reload — the cap is reachable,
  // and the eleventh call would come up with no speaking indicator at all.
  const audioCtxRef = useRef<AudioContext | null>(null)
  // Set the instant a start begins, cleared only by endCall(). See the
  // comment at the top of startCall() for why `status` cannot do this.
  const startingRef = useRef(false)
  // Blob URLs stay alive while their tab is open — revoking one immediately
  // after window.open() gives the user a blank viewer. Held here and
  // released together when the call ends.
  const blobUrlsRef = useRef<string[]>([])

  useEffect(() => {
    audioRef.current = new Audio()
    audioRef.current.autoplay = true
    // Deliberately no cleanup that ends the call: this provider unmounts
    // only when the whole app goes away, and the browser is tearing the
    // media down anyway at that point.
  }, [])

  // A live call is real-time media that cannot be restored on the next page
  // load, so closing the tab silently drops a caller mid-sentence. Now that
  // the call outlives every route, a stray Ctrl+W is the remaining way to
  // lose one by accident.
  useEffect(() => {
    if (status === 'idle' || status === 'error') return
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      // preventDefault() alone is the current spec, but Safari and older
      // WebKit only honour the legacy returnValue. Setting both is the only
      // way the prompt actually appears everywhere.
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [status])

  function addLog(msg: string) {
    setLog(prev => [...prev.slice(-50), msg])
  }

  async function startCall(target: Bot) {
    // Re-entrancy guard. `status` cannot do this job: setStatus is async, so
    // two clicks landing in the same React batch both read status==='idle'
    // and both proceed. The second run then overwrites pcRef.current, which
    // strands the FIRST RTCPeerConnection — still live, still sending the
    // mic, but no longer referenced anywhere the page can reach, so
    // endCall() cannot close it and the user cannot see it exists.
    // Root-caused 2026-09-03 from a call where exactly that happened: the
    // server ran two pipelines for one caller and the two bots talked over
    // each other. A ref is checked and set synchronously, so it actually
    // closes the window that `status` leaves open.
    if (startingRef.current) return
    startingRef.current = true
    // Belt and braces — never build a second connection on top of a live
    // one, whatever route got us here. closeConnection() rather than
    // endCall() on purpose: endCall clears the guard we just set, which
    // would hand the very race above back to the second click.
    closeConnection()
    setBot(target)
    setStatus('connecting')
    setMuted(false)
    setConnectedAt(null)
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
          // Only set once. 'connected' can fire again after a recovered
          // blip, and restarting the timer there would tell the operator a
          // twelve-minute call had lasted ten seconds.
          setConnectedAt(prev => prev ?? Date.now())
          addLog('Ready — speak now')
        }
        if (pc.iceConnectionState === 'failed' || pc.iceConnectionState === 'disconnected') {
          addLog('Connection lost')
          // Release the start guard here, or the Start button this error
          // state puts back on screen is dead. The guard is otherwise
          // cleared only by endCall(), and nothing in this path calls it:
          // the connection dropped on its own rather than being stopped.
          // startCall() would then return immediately on a guard nothing
          // can clear, and the only way back would be a page reload. This
          // path matters — a dropped network, a failed ICE negotiation, or
          // the server ending a stale call all land here.
          //
          // Teardown is deliberately left to startCall()'s own
          // closeConnection(): 'disconnected' can recover to 'connected' on
          // its own, and closing the peer here would make a recoverable
          // blip permanent.
          startingRef.current = false
          setStatus('error')
        }
      }

      const ctx = new AudioContext()
      audioCtxRef.current = ctx
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
      const answer = await connectBot(target.id, pc.localDescription!.sdp, pc.localDescription!.type)
      await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type as RTCSdpType })

      pcId = answer.pc_id
      if (pending.length) {
        sendIceCandidates(pcId, pending.splice(0))
      }
      addLog('Handshake complete ✓')
    } catch (e: any) {
      addLog(`Error: ${e.message}`)
      // Order matters, and it was wrong before: endCall() sets the status to
      // 'idle', so setting 'error' first meant the teardown immediately
      // overwrote it and a denied microphone rendered as "Press Start to
      // begin" — the failure was in the log and nowhere else. Tear down
      // first, then say what happened.
      endCall()
      setStatus('error')
    }
  }

  function endCall() {
    // Every route out of a live/connecting call comes through here — the
    // End button on the page, the End button on the call bar, and the error
    // path — so this is the one place the start guard is released. While
    // connected it deliberately stays set, which also blocks a second Start
    // on top of a live call.
    startingRef.current = false
    closeConnection()
    setStatus('idle')
    setSpeaking(false)
    setMuted(false)
    setConnectedAt(null)
    // `bot` is deliberately left alone: SessionPage still wants to show
    // whose call just ended, and startCall() replaces it on the next one.
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
    if (audioCtxRef.current) {
      // Fire and forget; a context that is already closing rejects, and
      // there is nothing useful to do about it either way.
      audioCtxRef.current.close().catch(() => {})
      audioCtxRef.current = null
    }
    if (audioRef.current) audioRef.current.srcObject = null
  }

  /**
   * Mute stops the caller being heard, and nothing else. The track is
   * disabled rather than stopped: stopping it would end the media
   * permanently and there would be no way back without renegotiating.
   * A disabled track keeps sending silence, so the call stays up.
   */
  function toggleMute() {
    const stream = streamRef.current
    if (!stream) return
    const next = !muted
    stream.getAudioTracks().forEach(t => { t.enabled = !next })
    setMuted(next)
    addLog(next ? 'Microphone muted' : 'Microphone unmuted')
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

  return (
    <CallContext.Provider value={{
      bot, status, speaking, muted, log, sources, connectedAt, openingDoc,
      startCall, endCall, toggleMute, openSource,
    }}>
      {children}
    </CallContext.Provider>
  )
}

export function useCall() {
  const ctx = useContext(CallContext)
  if (!ctx) throw new Error('useCall must be used inside CallProvider')
  return ctx
}

/** True while a call is up or coming up — the condition the call bar shows on. */
export function isCallActive(status: CallStatus) {
  return status === 'connecting' || status === 'connected'
}
