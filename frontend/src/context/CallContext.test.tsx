/**
 * Review finding C3 (2026-09-10) — a call ended mid-connect left the
 * microphone live.
 *
 * startCall() suspends on four awaits, and connecting takes seconds.
 * endCall() ran closeConnection(), which can only tear down the refs that
 * are assigned at that instant — so a stream granted one line later, or a
 * peer built one line later, was invisible to it and ran on unreferenced:
 * no End button, no call bar, and the browser's recording indicator still
 * on. Signing out was the same bug with the session already gone.
 *
 * These tests drive that window directly by holding each await open and
 * ending the call inside it. The assertions are deliberately about the
 * resources, not the UI: `track.stop()` was called, the peer was closed,
 * the answer was never applied. That is what "the mic is off" means.
 *
 * jsdom implements none of WebRTC or Web Audio, so both are stubbed below.
 * That is honest here — the code under test is the bookkeeping around those
 * objects, and the stubs record exactly the calls the bookkeeping must make.
 */

import { render, act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CallProvider, useCall } from './CallContext'
import type { Bot } from '../lib/api'

vi.mock('../lib/api', () => ({
  getIceServers: vi.fn(),
  connectBot: vi.fn(),
  sendIceCandidates: vi.fn(),
  fetchDocumentBlobUrl: vi.fn(),
}))

import { connectBot, getIceServers } from '../lib/api'

// ---------------------------------------------------------------- fixtures

/** A promise whose settlement this test controls, to hold an await open. */
function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  // The rejection paths below are settled after the call has already been
  // ended, so nothing is listening yet when they reject. Attach a no-op
  // handler so node doesn't count that as an unhandled rejection.
  promise.catch(() => {})
  return { promise, resolve, reject }
}

interface FakeTrack { kind: string; enabled: boolean; stopped: boolean; stop(): void }

function fakeStream() {
  const track: FakeTrack = {
    kind: 'audio',
    enabled: true,
    stopped: false,
    stop() { this.stopped = true },
  }
  return {
    track,
    stream: {
      getTracks: () => [track],
      getAudioTracks: () => [track],
    } as unknown as MediaStream,
  }
}

class FakePeerConnection {
  static made: FakePeerConnection[] = []
  closed = false
  localDescription: unknown = null
  /** Every answer actually applied. Must stay empty on an abandoned call. */
  applied: unknown[] = []
  iceConnectionState = 'new'
  ontrack: unknown = null
  onicecandidate: unknown = null
  oniceconnectionstatechange: unknown = null

  constructor() { FakePeerConnection.made.push(this) }
  addTrack() {}
  createOffer() { return Promise.resolve({ sdp: 'v=0', type: 'offer' }) }
  setLocalDescription(d: unknown) { this.localDescription = { sdp: 'v=0', type: 'offer', ...(d as object) }; return Promise.resolve() }
  setRemoteDescription(d: unknown) { this.applied.push(d); return Promise.resolve() }
  createDataChannel() { return { close() {}, onopen: null, onmessage: null } }
  close() { this.closed = true }
}

class FakeAudioContext {
  static made: FakeAudioContext[] = []
  closed = false
  constructor() { FakeAudioContext.made.push(this) }
  createAnalyser() {
    return { frequencyBinCount: 8, getByteFrequencyData() {} }
  }
  createMediaStreamSource() { return { connect() {} } }
  close() { this.closed = true; return Promise.resolve() }
}

const BOT = { id: 'bot-1', name: 'Support' } as unknown as Bot

// ------------------------------------------------------------------ harness

type Call = ReturnType<typeof useCall>
let call: Call

function Probe() {
  call = useCall()
  return null
}

function mount() {
  render(<CallProvider><Probe /></CallProvider>)
}

/** Let every pending microtask and timer callback settle inside act(). */
async function settle() {
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

beforeEach(() => {
  FakePeerConnection.made = []
  FakeAudioContext.made = []
  vi.stubGlobal('RTCPeerConnection', FakePeerConnection)
  vi.stubGlobal('AudioContext', FakeAudioContext)
  // The real one reschedules itself forever; the speaking indicator is not
  // what these tests are about, so one pass and stop.
  vi.stubGlobal('requestAnimationFrame', () => 0)
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: vi.fn() },
  })
  vi.mocked(getIceServers).mockResolvedValue([])
  vi.mocked(connectBot).mockResolvedValue({ sdp: 'v=0', type: 'answer', pc_id: 'pc-1' } as never)
})

// Registered after the stubs above so every test mounts against them.
beforeEach(mount)

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

// -------------------------------------------------------------------- tests

describe('ending a call while it is still connecting', () => {
  it('stops a microphone granted after the call was ended', async () => {
    const gum = deferred<MediaStream>()
    vi.mocked(navigator.mediaDevices.getUserMedia).mockReturnValue(gum.promise)

    let started!: Promise<void>
    await act(async () => { started = call.startCall(BOT) })
    // Suspended on the permission prompt: nothing exists yet for
    // closeConnection() to find, which is exactly the hole.
    expect(FakePeerConnection.made).toHaveLength(0)

    act(() => { call.endCall() })

    const { track, stream } = fakeStream()
    await act(async () => { gum.resolve(stream); await started })

    expect(track.stopped).toBe(true)
    expect(FakePeerConnection.made).toHaveLength(0)
    expect(call.status).toBe('idle')
  })

  it('closes the peer and the mic when the call is ended during the handshake', async () => {
    const { track, stream } = fakeStream()
    vi.mocked(navigator.mediaDevices.getUserMedia).mockResolvedValue(stream)
    const answer = deferred<{ sdp: string; type: string; pc_id: string }>()
    vi.mocked(connectBot).mockReturnValue(answer.promise as never)

    let started!: Promise<void>
    await act(async () => { started = call.startCall(BOT) })
    await settle()

    // Far enough in that the microphone is live and the peer exists.
    const pc = FakePeerConnection.made[0]
    expect(pc).toBeDefined()
    expect(track.stopped).toBe(false)

    act(() => { call.endCall() })

    await act(async () => {
      answer.resolve({ sdp: 'v=0', type: 'answer', pc_id: 'pc-1' })
      await started
    })

    expect(track.stopped).toBe(true)
    expect(pc.closed).toBe(true)
    expect(FakeAudioContext.made[0].closed).toBe(true)
    // The answer belongs to a call nobody is on. Applying it would complete
    // the negotiation and put the caller back on the line.
    expect(pc.applied).toHaveLength(0)
    expect(call.status).toBe('idle')
  })

  it('does not report an error for a request that fails after sign-out', async () => {
    // Signing out calls endCall() and then drops the token, so the /connect
    // request already in flight comes back 401. That failure is about a call
    // that no longer exists and must not paint over the signed-out screen.
    const { track, stream } = fakeStream()
    vi.mocked(navigator.mediaDevices.getUserMedia).mockResolvedValue(stream)
    const answer = deferred<never>()
    vi.mocked(connectBot).mockReturnValue(answer.promise as never)

    let started!: Promise<void>
    await act(async () => { started = call.startCall(BOT) })
    await settle()

    act(() => { call.endCall() })
    await act(async () => {
      answer.reject(new Error('401 Unauthorized'))
      await started
    })

    expect(call.status).toBe('idle')
    expect(track.stopped).toBe(true)
    expect(FakePeerConnection.made[0].closed).toBe(true)
  })
})

describe('a call nobody interrupts', () => {
  it('still connects — the cancellation checks must not fire on their own', async () => {
    const { track, stream } = fakeStream()
    vi.mocked(navigator.mediaDevices.getUserMedia).mockResolvedValue(stream)

    await act(async () => { await call.startCall(BOT) })
    await settle()

    const pc = FakePeerConnection.made[0]
    expect(pc.closed).toBe(false)
    expect(track.stopped).toBe(false)
    expect(pc.applied).toHaveLength(1)   // the answer WAS applied
    expect(call.status).toBe('connecting')
    expect(call.bot?.id).toBe('bot-1')
  })

  it('tears everything down when it is ended normally', async () => {
    const { track, stream } = fakeStream()
    vi.mocked(navigator.mediaDevices.getUserMedia).mockResolvedValue(stream)

    await act(async () => { await call.startCall(BOT) })
    await settle()
    act(() => { call.endCall() })

    expect(track.stopped).toBe(true)
    expect(FakePeerConnection.made[0].closed).toBe(true)
    expect(FakeAudioContext.made[0].closed).toBe(true)
    expect(call.status).toBe('idle')
  })
})
