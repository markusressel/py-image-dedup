import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// the daemon serves the API on http://127.0.0.1:8080 by default (web.port),
// override with PY_IMAGE_DEDUP_API_URL when running `npm run dev`
const apiUrl = process.env.PY_IMAGE_DEDUP_API_URL ?? 'http://127.0.0.1:8080'

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '/api': apiUrl,
      '/metrics': apiUrl,
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
