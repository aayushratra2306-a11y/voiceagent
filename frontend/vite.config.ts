/// <reference types="vitest/config" />
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/auth': 'http://localhost:8080',
      '/bots': 'http://localhost:8080',
      '/connect': 'http://localhost:8080',
      '/documents': 'http://localhost:8080',
      // Task 9 — found while writing the local click-through script:
      // /orgs (list/create/rename/delete an organisation, its members) is
      // a real backend router (see app/api/orgs.py) but was never added
      // here. Every one of this feature's manual checks needs it — without
      // it `npm run dev` falls through to the SPA for every org call and
      // the page never gets past "loading". (deploy/Caddyfile's proxy list
      // had the same gap; that was outside frontend/, so out of scope here,
      // but it was fixed separately — commit 3a7becd.)
      '/orgs': 'http://localhost:8080',
      // Task 5.2 — AcceptInvitePage's GET /invitations/:token and POST
      // /invitations/:token/accept. Same gap as /orgs above: without this
      // entry `npm run dev` falls through to the SPA and the accept page
      // never gets past its loading state.
      '/invitations': 'http://localhost:8080',
    },
  },
  // jsdom rather than the default node environment: what is worth testing on
  // this side of the app is component and hook behaviour, and that needs a
  // document. The WebRTC and Web Audio APIs jsdom does NOT implement are
  // stubbed per-test — see src/context/CallContext.test.tsx — because the
  // point of those tests is the teardown bookkeeping around them, not the
  // browser's own media stack.
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
