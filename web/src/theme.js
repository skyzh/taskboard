import { useEffect, useLayoutEffect, useState } from 'react'

const storageKey = 'tasks-board.theme'
const darkQuery = '(prefers-color-scheme: dark)'

function explicitTheme(value) {
  return value === 'day' || value === 'night' ? value : ''
}

export function effectiveTheme(preference, systemDark) {
  return explicitTheme(preference) || (systemDark ? 'night' : 'day')
}

export function useTheme() {
  const [preference, setPreference] = useState(() => explicitTheme(document.documentElement.dataset.theme))
  const [systemDark, setSystemDark] = useState(() => window.matchMedia(darkQuery).matches)
  const mode = effectiveTheme(preference, systemDark)

  useEffect(() => {
    const query = window.matchMedia(darkQuery)
    const update = () => setSystemDark(query.matches)
    query.addEventListener('change', update)
    update()
    return () => query.removeEventListener('change', update)
  }, [])

  useLayoutEffect(() => {
    const root = document.documentElement
    if (preference) root.dataset.theme = preference
    else delete root.dataset.theme
    root.dataset.colorScheme = mode === 'night' ? 'dark' : 'light'
  }, [mode, preference])

  function chooseTheme(value) {
    const explicit = explicitTheme(value)
    setPreference(explicit)
    try {
      if (explicit) window.localStorage.setItem(storageKey, explicit)
      else window.localStorage.removeItem(storageKey)
    } catch {
      // The selection still applies to the current page.
    }
  }

  return { mode, preference, chooseTheme }
}
