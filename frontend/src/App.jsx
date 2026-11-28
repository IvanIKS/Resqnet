import { useState } from 'react'
import { AuthProvider, useAuth } from './context/AuthContext'
import AuthForm from './components/AuthForm'
import MapView from './components/MapView'
import './style.css'
import './App.css'

function AppContent() {
  const { user, loading, logout } = useAuth()
  const [selectedPoint, setSelectedPoint] = useState(null)

  if (loading) {
    return <div className="auth-page">Loading…</div>
  }

  if (!user) {
    return <AuthForm />
  }

  return (
    <main className="map-wrapper">
      <div className="map-user-pill">
        <span>
          {user.username} ({user.role})
        </span>
        <button type="button" className="map-logout-btn" onClick={logout}>
          Sign Out
        </button>
      </div>
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
