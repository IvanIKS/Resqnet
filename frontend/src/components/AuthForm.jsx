import { useState } from 'react'
import { useAuth } from '../context/AuthContext'

export default function AuthForm() {
  const { login, register } = useAuth()
  const [mode, setMode] = useState('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState('mobile_user')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      if (mode === 'login') {
        await login(username, password)
      } else {
        await register(username, password, role)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="auth-page">
      <form className="auth-card" onSubmit={handleSubmit}>
        <h1>ResQNet</h1>
        <p className="auth-subtitle">
          {mode === 'login' ? 'Sign in to continue' : 'Create an account'}
        </p>

        {error && <div className="auth-error">{error}</div>}

        <label className="auth-field">
          <span>Username</span>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
            minLength={3}
            autoComplete="username"
          />
        </label>

        <label className="auth-field">
          <span>Password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={6}
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
          />
        </label>

        {mode === 'register' && (
          <label className="auth-field">
            <span>Role</span>
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="mobile_user">Mobile User</option>
              <option value="dispatcher">Dispatcher</option>
            </select>
          </label>
        )}

        <button type="submit" className="auth-submit" disabled={submitting}>
          {submitting ? 'Please wait…' : mode === 'login' ? 'Sign In' : 'Register'}
        </button>

        <button
          type="button"
          className="auth-toggle"
          onClick={() => {
            setError('')
            setMode(mode === 'login' ? 'register' : 'login')
          }}
        >
          {mode === 'login' ? 'Need an account? Register' : 'Already have an account? Sign In'}
        </button>
      </form>
    </div>
  )
}
