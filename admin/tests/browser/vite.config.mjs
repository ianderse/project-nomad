import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath } from 'node:url'
import { maplibreWorker } from '../../vite/maplibre_worker.ts'

export default defineConfig({
  root: fileURLToPath(new URL('./fixture/', import.meta.url)),
  plugins: [react(), tailwindcss(), maplibreWorker()],
  resolve: { alias: { '~/': fileURLToPath(new URL('../../inertia/', import.meta.url)) } },
  build: { outDir: fileURLToPath(new URL('../../build/browser-regression/', import.meta.url)), assetsDir: '', emptyOutDir: true },
  preview: { host: '127.0.0.1', port: 4173, strictPort: true },
})
