import React, { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import Map, { NavigationControl } from 'react-map-gl/maplibre'
import * as maplibre from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import SupplyDepotCard from '~/components/SupplyDepotCard'
import StyledButton from '~/components/StyledButton'
import './style.css'

const record = {
  service_name: 'nomad_browser_fixture', friendly_name: 'Fixture App', description: 'A local test app',
  installed: true, status: 'running', category: 'utility', is_custom: true,
  container_image: 'fixture:1', ui_location: '/fixture', auto_update_enabled: false,
}
const style = {
  version: 8,
  sources: { fixture: { type: 'geojson', data: { type: 'FeatureCollection', features: [
    { type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: [[-106,39],[-104,41]] } },
  ] } } },
  layers: [
    { id: 'background', type: 'background', paint: { 'background-color': '#e4eddf' } },
    { id: 'road', type: 'line', source: 'fixture', paint: { 'line-color': '#a84a12', 'line-width': 8 } },
  ],
}

function Harness() {
  const [theme, setTheme] = useState('light')
  const [action, setAction] = useState('none')
  const [zoom, setZoom] = useState(6)
  const [features, setFeatures] = useState(0)
  const map = useRef(null)
  useEffect(() => { document.documentElement.dataset.theme = theme }, [theme])
  const handlers = {
    onInstall: () => setAction('install'), onForceReinstall: () => setAction('reinstall'),
    onUpdate: () => setAction('update'), onAffect: (_, action) => setAction(action),
    onEditCustom: () => setAction('edit'), onPullLatest: () => setAction('pull'),
    onViewLogs: () => setAction('logs'), onDeleteCustom: () => setAction('delete'),
    onToggleAutoUpdate: () => setAction('toggle'),
  }
  return <main className="p-4">
    <StyledButton onClick={() => setTheme(t => t === 'light' ? 'dark' : 'light')}>Toggle theme</StyledButton>
    <output data-testid="action">{action}</output>
    <div data-testid="clipped-parent" style={{ width: 'min(350px, 100%)', height: 240, overflow: 'hidden', marginTop: 16 }}>
      <SupplyDepotCard record={record} isNativeOllama={false} isOnline={true} isInstalling={false}
        loading={false} handlers={handlers} />
    </div>
    <div style={{ position: 'relative', height: 80, background: 'var(--color-surface-secondary)' }}>Sibling surface</div>
    <section style={{ height: 260 }} aria-label="Map fixture">
      <Map ref={map} mapLib={maplibre} mapStyle={style} initialViewState={{longitude:-105,latitude:40,zoom:6}}
        onMoveEnd={e => setZoom(e.viewState.zoom)}
        onIdle={() => setFeatures(map.current?.getMap().queryRenderedFeatures({layers:['road']}).length || 0)}>
        <NavigationControl />
      </Map>
    </section>
    <StyledButton onClick={() => map.current?.flyTo({center:[-104.9,39.8],zoom:8,duration:0})}>Fly to location</StyledButton>
    <output data-testid="zoom">{zoom}</output>
    <output data-testid="features">{features}</output>
  </main>
}
createRoot(document.getElementById('root')).render(<Harness />)
