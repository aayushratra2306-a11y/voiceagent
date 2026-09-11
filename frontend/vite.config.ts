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
