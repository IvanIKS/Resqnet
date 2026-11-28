import { useEffect, useRef } from 'react'

const WORDS = ['FLOOD', 'WAVE', 'WATER', 'SOS', 'EVAC', '01', '10', '~~~', '≈≈≈']

export default function TerminalBackground() {
  const canvasRef = useRef(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    const styles = getComputedStyle(document.documentElement)
    const bg = styles.getPropertyValue('--color-bg').trim() || '#f8fafc'
    const waveColor = styles.getPropertyValue('--color-primary').trim() || '#2563eb'
    const foamColor = styles.getPropertyValue('--color-terminal-char').trim() || '#94a3b8'
    const fontFamily = styles.getPropertyValue('--font-mono').trim() || 'monospace'

    const cellW = 14
    const cellH = 18
    let cols = 0
    let rows = 0

    function resize() {
      canvas.width = window.innerWidth
      canvas.height = window.innerHeight
      cols = Math.ceil(canvas.width / cellW)
      rows = Math.ceil(canvas.height / cellH)
    }

    resize()
    window.addEventListener('resize', resize)

    let frameId
    let lastTime = 0

    function draw(time) {
      if (time - lastTime > 28) {
        lastTime = time
        ctx.fillStyle = bg
        ctx.fillRect(0, 0, canvas.width, canvas.height)
        ctx.font = `13px ${fontFamily}`

        const surge = (Math.sin(time * 0.00045) + 1) * 0.5
        const baseLevel = rows * (0.85 - surge * 0.65)

        for (let c = 0; c < cols; c++) {
          const waveOffset =
            Math.sin(c * 0.22 + time * 0.003) * 2.4 +
            Math.cos(c * 0.11 - time * 0.002) * 1.6
          const surfaceRow = baseLevel + waveOffset

          for (let r = Math.max(0, Math.floor(surfaceRow)); r < rows; r++) {
            const depth = r - surfaceRow
            const isCrest = depth < 1.5
            ctx.fillStyle = isCrest ? foamColor : waveColor
            ctx.globalAlpha = isCrest ? 0.55 : Math.min(0.32, 0.1 + depth * 0.015)

            const word = WORDS[(c + r + Math.floor(time * 0.004)) % WORDS.length]
            const ch = word[(c + r) % word.length]
            ctx.fillText(ch, c * cellW, r * cellH)
          }
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
