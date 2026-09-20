import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider, useAuth } from './context/AuthContext'
import { CallProvider } from './context/CallContext'
import { ChromeProvider } from './context/ChromeContext'
import OrgProvider from './context/OrgContext'
import AppShell from './components/AppShell'
import RedirectToOrg from './components/RedirectToOrg'
import LoginPage from './pages/LoginPage'
import ChooseOrgPage from './pages/ChooseOrgPage'
import DashboardPage from './pages/DashboardPage'
import BotSettingsPage from './pages/BotSettingsPage'
import BotToolsPage from './pages/BotToolsPage'
import SessionPage from './pages/SessionPage'
import WebhooksPage from './pages/WebhooksPage'
import ApprovalsPage from './pages/ApprovalsPage'
import MembersPage from './pages/MembersPage'
import OrgSettingsPage from './pages/OrgSettingsPage'

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

              {/* Everything signed-in lives under /o/:orgId, so the address
                  says which organisation you are looking at and a link you
                  paste to a colleague opens the same workspace. OrgProvider
                  is inside PrivateRoute because it calls GET /orgs. */}
              <Route element={<PrivateRoute><OrgProvider /></PrivateRoute>}>
                <Route element={<AppShell />}>
                  <Route path="/o/:orgId/dashboard" element={<DashboardPage />} />
                  <Route path="/o/:orgId/bots/:id" element={<BotSettingsPage />} />
                  <Route path="/o/:orgId/bots/:id/tools" element={<BotToolsPage />} />
                  <Route path="/o/:orgId/session/:id" element={<SessionPage />} />
                  <Route path="/o/:orgId/webhooks" element={<WebhooksPage />} />
                  <Route path="/o/:orgId/approvals" element={<ApprovalsPage />} />
                  <Route path="/o/:orgId/members" element={<MembersPage />} />
                  <Route path="/o/:orgId/settings" element={<OrgSettingsPage />} />
                </Route>
              </Route>

              {/* The chooser sits outside OrgProvider — it is what you see
                  when there is no organisation to provide. */}
              <Route path="/o" element={<PrivateRoute><ChooseOrgPage /></PrivateRoute>} />

              {/* Addresses from before 5.1. Each lands on the same page
                  under the last-used organisation. */}
              <Route path="/dashboard" element={<PrivateRoute><RedirectToOrg /></PrivateRoute>} />
              <Route path="/bots/:id" element={<PrivateRoute><RedirectToOrg /></PrivateRoute>} />
              <Route path="/bots/:id/tools" element={<PrivateRoute><RedirectToOrg /></PrivateRoute>} />
              <Route path="/session/:id" element={<PrivateRoute><RedirectToOrg /></PrivateRoute>} />
              <Route path="/webhooks" element={<PrivateRoute><RedirectToOrg /></PrivateRoute>} />
              <Route path="/approvals" element={<PrivateRoute><RedirectToOrg /></PrivateRoute>} />
            </Routes>
          </ChromeProvider>
        </CallProvider>
      </BrowserRouter>
    </AuthProvider>
  )
}
