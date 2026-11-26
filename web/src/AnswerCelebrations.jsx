import React, { createContext, useContext, useEffect, useState } from 'react'
import './answer-celebrations.css'

const preferenceKey = 'taskboard-answer-celebrations'
const AnswerCelebrations = createContext({ enabled: false, choose() {}, celebrate() {} })
const colors = ['#f9c74f', '#ff6d91', '#49dcb1', '#9f88ff', '#54c9ff']
const pieces = Array.from({ length: 36 }, (_, index) => {
  const angle = index * Math.PI * 2 / 36
  const distance = [32, 49, 40][index % 3]
  return {
    '--x': `${Math.cos(angle) * distance}vw`,
    '--y': `${Math.sin(angle) * distance}vh`,
    '--turn': `${(index % 2 ? -1 : 1) * (200 + index * 19)}deg`,
    '--color': colors[index % colors.length],
  }
})

export function useAnswerCelebrations() {
  return useContext(AnswerCelebrations)
}

export function AnswerCelebrationsProvider({ children }) {
  const [enabled, setEnabled] = useState(() => {
    try {
      return window.localStorage.getItem(preferenceKey) === 'true'
    } catch {
      return false
    }
  })
  const [burst, setBurst] = useState(0)

  function choose(next) {
    setEnabled(next)
    if (!next) setBurst(0)
    try {
      if (next) window.localStorage.setItem(preferenceKey, 'true')
      else window.localStorage.removeItem(preferenceKey)
    } catch {
      // The preference still applies to the current page.
    }
  }

  function celebrate() {
    if (enabled && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setBurst(current => current + 1)
    }
  }

  useEffect(() => {
    if (!burst) return
    const timer = setTimeout(() => setBurst(0), 1250)
    return () => clearTimeout(timer)
  }, [burst])

  return (
    <AnswerCelebrations.Provider value={{ enabled, choose, celebrate }}>
      {children}
      {burst > 0 && <div key={burst} className="answer-celebration" aria-hidden="true">
        <span className="answer-celebration-ring" />
        {pieces.map((style, index) => <i key={index} style={style} />)}
      </div>}
    </AnswerCelebrations.Provider>
  )
}
