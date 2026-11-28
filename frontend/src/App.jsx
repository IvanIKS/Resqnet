import { useState } from 'react'
import { AuthProvider, useAuth } from './context/AuthContext'
import AuthForm from './components/AuthForm'
import MapView from './components/MapView'
import './style.css'
import './App.css'

function AppContent() {
  const { user, loading } = useAuth()
  const [selectedPoint, setSelectedPoint] = useState(null)

  if (loading) {
    return <div className="auth-page">Loading…</div>
  }

  if (!user) {
    return <AuthForm />
  }

  return (
    <main className="map-wrapper">
      <MapView selectedPoint={selectedPoint} onSelectPoint={setSelectedPoint} />
    </main>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <AppContent />
    </AuthProvider>
  )
}
