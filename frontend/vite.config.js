import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Backend the dev server forwards /api and /static to. Override with BACKEND_URL
// if port 8000 is taken, e.g. BACKEND_URL=http://localhost:8001
const backend = process.env.BACKEND_URL || 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api':    { target: backend, changeOrigin: true },
      '/static': { target: backend, changeOrigin: true },
    },
  },
})
