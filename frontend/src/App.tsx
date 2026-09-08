import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider, useAuth } from './context/AuthContext'
import { CallProvider } from './context/CallContext'
import { ChromeProvider } from './context/ChromeContext'
import AppShell from './components/AppShell'
import LoginPage from './pages/LoginPage'
import DashboardPage from './pages/DashboardPage'
import BotSettingsPage from './pages/BotSettingsPage'
import BotToolsPage from './pages/BotToolsPage'
import SessionPage from './pages/SessionPage'
import WebhooksPage from './pages/WebhooksPage'
import ApprovalsPage from './pages/ApprovalsPage'

function PrivateRoute({ children }: { children: React.ReactNode }) {
  const { token, ready } = useAuth()
  // Task 2.5 — wait for the on-load silent-refresh attempt to resolve
  // before deciding: `token` can still be null for a moment on a fresh
  // tab even for someone with a perfectly valid session (their access
  // token lives in an httpOnly cookie the app hasn't exchanged yet), and
  // bouncing to /login during that window would just be a wrong flicker.
  if (!ready) return null
  return token ? <>{children}</> : <Navigate to="/" replace />
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        {/* Both providers sit ABOVE <Routes> on purpose. CallProvider holds
            the peer connection, so the call is no longer owned by a page and
            no longer dies when one unmounts; ChromeProvider lets a page tell
            the shell what its header should say without owning a header. */}
        <CallProvider>
          <ChromeProvider>
            <Routes>
              <Route path="/" element={<LoginPage />} />
              {/* One layout route. Everything signed-in renders inside the
                  shell, which is why every page now has the same background,
                  the same navigation and the same approvals badge. */}
              <Route element={<PrivateRoute><AppShell /></PrivateRoute>}>
                <Route path="/dashboard" element={<DashboardPage />} />
                <Route path="/bots/:id" element={<BotSettingsPage />} />
                <Route path="/bots/:id/tools" element={<BotToolsPage />} />
                <Route path="/session/:id" element={<SessionPage />} />
                <Route path="/webhooks" element={<WebhooksPage />} />
                <Route path="/approvals" element={<ApprovalsPage />} />
              </Route>
            </Routes>
          </ChromeProvider>
        </CallProvider>
      </BrowserRouter>
    </AuthProvider>
  )
}
