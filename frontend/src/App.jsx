import { useState } from 'react'
import { AuthProvider, useAuth } from './context/AuthContext'
import AuthForm from './components/AuthForm'
import MapView from './components/MapView'
import './style.css'
import './App.css'

function AppContent() {
  const { user, logout } = useAuth()
  const [selectedPoint, setSelectedPoint] = useState(null)
  const [showAuth, setShowAuth] = useState(false)

  return (
    <main className="map-wrapper">
      <div className="map-user-pill">
        {user ? (
          <>
            <span>
              {user.username} ({user.role})
            </span>
            <button type="button" className="map-auth-btn" onClick={logout}>
              Sign Out
            </button>
          </>
        ) : (
          <button
            type="button"
            className="map-auth-btn"
            onClick={() => setShowAuth(true)}
          >
            Sign In
          </button>
        )}
      </div>

      <MapView selectedPoint={selectedPoint} onSelectPoint={setSelectedPoint} />

      {showAuth && !user && (
        <div className="auth-modal-backdrop">
          <AuthForm onClose={() => setShowAuth(false)} />
        </div>
      )}
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
