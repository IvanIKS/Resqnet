import { AuthProvider, useAuth } from './context/AuthContext'
import AuthForm from './components/AuthForm'
import './style.css'
import './App.css'

function AppContent() {
  const { user, loading, logout } = useAuth()

  if (loading) {
    return <div className="auth-page">Loading…</div>
  }

  if (!user) {
    return <AuthForm />
  }

  return (
    <main className="app">
      <h1>ResQNet</h1>
      <p>
        Signed in as <strong>{user.username}</strong> ({user.role})
      </p>
      <button type="button" className="logout-btn" onClick={logout}>
        Sign Out
      </button>
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
