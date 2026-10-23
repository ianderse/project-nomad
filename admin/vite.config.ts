import { defineConfig } from 'vite'
import { getDirname } from '@adonisjs/core/helpers'
import inertia from '@adonisjs/inertia/client'
import react from '@vitejs/plugin-react'
import adonisjs from '@adonisjs/vite/client'
import tailwindcss from '@tailwindcss/vite'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

export default defineConfig({
  plugins: [
    inertia({ ssr: { enabled: false } }),
    react(),
    tailwindcss(),
    adonisjs({ entrypoints: ['inertia/app/app.tsx'], reload: ['resources/views/**/*.edge'] }),
    {
      name: 'nomad-maplibre-worker',
      apply: 'build',
      generateBundle() {
        // MapLibre 6 resolves this module beside the generated map chunk.
        // Vite cannot discover its runtime URL, so publish the matching worker.
        this.emitFile({
          type: 'asset',
          fileName: 'maplibre-gl-worker.mjs',
          source: readFileSync(require.resolve('maplibre-gl/dist/maplibre-gl-worker.mjs')),
        })
      },
    },
  ],

  /**
   * Define aliases for importing modules from
   * your frontend code
   */
  resolve: {
    alias: {
      '~/': `${getDirname(import.meta.url)}/inertia/`,
    },
  },
})
