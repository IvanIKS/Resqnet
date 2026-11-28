import { useEffect, useRef } from 'react'

const TOKENS = [
  '0', '1', '50.45', '30.52', 'SOS', 'EVAC', 'FLOOD', 'ROUTE', 'SAFE', 'GRID', 'WKT', '4326',
]

export default function TerminalBackground() {
  const canvasRef = useRef(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    const styles = getComputedStyle(document.documentElement)
    const bg = styles.getPropertyValue('--color-bg').trim() || '#f8fafc'
    const charColor = styles.getPropertyValue('--color-terminal-char').trim() || '#94a3b8'
    const fontFamily = styles.getPropertyValue('--font-mono').trim() || 'monospace'

    const fontSize = 14
    let columns = 0
    let drops = []

    function resize() {
      canvas.width = window.innerWidth
      canvas.height = window.innerHeight
      columns = Math.floor(canvas.width / fontSize)
      drops = Array.from({ length: columns }, () => Math.random() * -40)
    }

    resize()
    window.addEventListener('resize', resize)

    let frameId
    let lastTime = 0

    function draw(time) {
      if (time - lastTime > 55) {
        lastTime = time
        ctx.fillStyle = bg
        ctx.globalAlpha = 0.16
        ctx.fillRect(0, 0, canvas.width, canvas.height)

        ctx.globalAlpha = 0.45
        ctx.fillStyle = charColor
        ctx.font = `${fontSize}px ${fontFamily}`

        for (let i = 0; i < drops.length; i++) {
          const token = TOKENS[Math.floor(Math.random() * TOKENS.length)]
          ctx.fillText(token, i * fontSize * 3.2, drops[i] * fontSize)
          if (drops[i] * fontSize > canvas.height && Math.random() > 0.975) {
            drops[i] = 0
          }
          drops[i] += 0.55
        }
        ctx.globalAlpha = 1
      }
      frameId = requestAnimationFrame(draw)
    }

    frameId = requestAnimationFrame(draw)
    return () => {
      cancelAnimationFrame(frameId)
      window.removeEventListener('resize', resize)
    }
  }, [])

  return <canvas ref={canvasRef} className="terminal-bg" aria-hidden="true" />
}
