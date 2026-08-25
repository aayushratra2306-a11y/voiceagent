import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { listBots, connectBot } from '../lib/api'
import type { Bot } from '../lib/api'

type Status = 'idle' | 'connecting' | 'connected' | 'error'

export default function SessionPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [bot, setBot] = useState<Bot | null>(null)
  const [status, setStatus] = useState<Status>('idle')
  const [log, setLog] = useState<string[]>([])
  const [speaking, setSpeaking] = useState(false)
  const pcRef = useRef<RTCPeerConnection | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

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
    setStatus('connecting')
    setLog([])
    addLog('Requesting microphone…')
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false })
      streamRef.current = stream

      const pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] })
      pcRef.current = pc

      stream.getTracks().forEach(t => pc.addTrack(t, stream))

      pc.ontrack = e => {
        if (audioRef.current) audioRef.current.srcObject = e.streams[0]
        addLog('Bot audio connected ✓')
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

      addLog('Creating WebRTC offer…')
      await pc.setLocalDescription(await pc.createOffer())

      await new Promise<void>(resolve => {
        if (pc.iceGatheringState === 'complete') { resolve(); return }
        pc.onicegatheringstatechange = () => { if (pc.iceGatheringState === 'complete') resolve() }
        setTimeout(resolve, 5000)
      })

      addLog('Connecting to bot…')
      const answer = await connectBot(id!, pc.localDescription!.sdp, pc.localDescription!.type)
      await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type as RTCSdpType })
      addLog('Handshake complete ✓')
    } catch (e: any) {
      addLog(`Error: ${e.message}`)
      setStatus('error')
      stopSession()
    }
  }

  function stopSession() {
    pcRef.current?.close()
    pcRef.current = null
    streamRef.current?.getTracks().forEach(t => t.stop())
    streamRef.current = null
    if (audioRef.current) audioRef.current.srcObject = null
    setStatus('idle')
    setSpeaking(false)
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
