import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import * as Sentry from '@sentry/react'
import './index.css'
import App from './App'

// Task 2.7 — error tracking, browser half. The backend half only ever sees
// server-side failures; a React crash, a failed fetch or a getUserMedia
// rejection never reaches it, and those are exactly the failures a user
// actually experiences during a call.
//
// Dormant unless VITE_SENTRY_DSN is set at build time: Sentry.init with an
// empty dsn is a no-op, so a build with no DSN configured behaves exactly
// as it did before this existed. Vite inlines VITE_* vars at build time,
// so this is baked into the bundle rather than read at runtime.
//
// This should be its OWN Sentry project, not the backend's — mixing a
// browser and a Python service in one project makes both harder to read,
// and the platform-specific issue grouping stops working properly.
const dsn = import.meta.env.VITE_SENTRY_DSN ?? ''

if (dsn) {
  Sentry.init({
    dsn,
    // No Session Replay and no tracing integrations: both are quota-hungry
    // on the free tier, and this app's hard problems are audio/WebRTC ones
    // that a replay wouldn't capture anyway.
    integrations: [],
    // Voice calls carry real customer conversation data. Never let the SDK
    // attach identifying request details by default.
    sendDefaultPii: false,
  })
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* Catches render-time crashes that would otherwise leave a blank page
        with no clue what happened — for the user or for us. */}
    <Sentry.ErrorBoundary
      fallback={
        <div style={{
          minHeight: '100vh', display: 'flex', alignItems: 'center',
          justifyContent: 'center', background: '#070711', color: '#cbd5e1',
          fontFamily: 'system-ui, sans-serif', textAlign: 'center', padding: '2rem',
        }}>
          <div>
            <p style={{ fontWeight: 600, marginBottom: '0.5rem' }}>Something went wrong.</p>
            <p style={{ fontSize: '0.875rem', color: '#64748b' }}>
              Please reload the page. If it keeps happening, the problem has been reported.
            </p>
          </div>
        </div>
      }
    >
      <App />
    </Sentry.ErrorBoundary>
  </StrictMode>,
)
