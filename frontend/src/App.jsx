import { useState } from 'react'
import MapView from './components/MapView'
import './App.css'

export default function App() {
  const [selectedPoint, setSelectedPoint] = useState(null)

  return (
    <main className="map-wrapper">
      <MapView selectedPoint={selectedPoint} onSelectPoint={setSelectedPoint} />
    </main>
  )
}
