import { MapContainer, Marker, Popup, TileLayer, useMapEvents } from 'react-leaflet'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import markerIcon2x from 'leaflet/dist/images/marker-icon-2x.png'
import markerIcon from 'leaflet/dist/images/marker-icon.png'
import markerShadow from 'leaflet/dist/images/marker-shadow.png'

delete L.Icon.Default.prototype._getIconUrl
L.Icon.Default.mergeOptions({
  iconRetinaUrl: markerIcon2x,
  iconUrl: markerIcon,
  shadowUrl: markerShadow,
})

const DEFAULT_CENTER = [50.4501, 30.5234]

function LocationPicker({ onSelect }) {
  useMapEvents({
    click(e) {
      onSelect({ lat: e.latlng.lat, lon: e.latlng.lng })
    },
  })
  return null
}

export default function MapView({ selectedPoint, onSelectPoint }) {
  return (
    <MapContainer center={DEFAULT_CENTER} zoom={12} className="map-container">
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
      />
      <LocationPicker onSelect={onSelectPoint} />
      {selectedPoint && (
        <Marker position={[selectedPoint.lat, selectedPoint.lon]}>
          <Popup>
            {selectedPoint.lat.toFixed(5)}, {selectedPoint.lon.toFixed(5)}
          </Popup>
        </Marker>
      )}
    </MapContainer>
  )
}
