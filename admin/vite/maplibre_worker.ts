import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import type { Plugin } from 'vite'

const require = createRequire(import.meta.url)

export function maplibreWorker(): Plugin {
  return {
    name: 'nomad-maplibre-worker',
    apply: 'build',
    generateBundle() {
      this.emitFile({
        type: 'asset',
        fileName: 'maplibre-gl-worker.mjs',
        source: readFileSync(require.resolve('maplibre-gl/dist/maplibre-gl-worker.mjs')),
      })
    },
  }
}
