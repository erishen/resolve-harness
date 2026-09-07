import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev: Vite on :5175, proxies /api to the FastAPI backend on :8899.
// base './' so the built dist/index.html also opens directly from the
// filesystem (no server) — handy for static previews.
export default defineConfig({
  base: './',
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5175,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8899',
        changeOrigin: true,
      },
    },
  },
})
