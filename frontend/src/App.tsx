import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider, useAuth } from './context/AuthContext'
import LoginPage from './pages/LoginPage'
import DashboardPage from './pages/DashboardPage'
import BotSettingsPage from './pages/BotSettingsPage'
import BotToolsPage from './pages/BotToolsPage'
import SessionPage from './pages/SessionPage'
import WebhooksPage from './pages/WebhooksPage'

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
        <Routes>
          <Route path="/" element={<LoginPage />} />
          <Route path="/dashboard" element={<PrivateRoute><DashboardPage /></PrivateRoute>} />
          <Route path="/bots/:id" element={<PrivateRoute><BotSettingsPage /></PrivateRoute>} />
          <Route path="/bots/:id/tools" element={<PrivateRoute><BotToolsPage /></PrivateRoute>} />
          <Route path="/session/:id" element={<PrivateRoute><SessionPage /></PrivateRoute>} />
          <Route path="/webhooks" element={<PrivateRoute><WebhooksPage /></PrivateRoute>} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  )
}
