import { useEffect, useState } from 'react'
import './App.css'

export default function App() {
  const [status, setStatus] = useState('checking…')

  // Ping the backend so we can see the dev proxy is wired up.
  useEffect(() => {
    fetch('/api/health')
      .then((res) => (res.ok ? setStatus('online') : setStatus('error')))
      .catch(() => setStatus('offline'))
  }, [])

  return (
    <main className="app">
      <h1>ResQNet</h1>
      <p>Disaster response coordination — flood operations.</p>
      <p className="status">
        Backend: <span className={`badge ${status}`}>{status}</span>
      </p>
    </main>
  )
}
