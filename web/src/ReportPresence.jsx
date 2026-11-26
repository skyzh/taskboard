import React, { useEffect, useState } from 'react'

const renewEvery = 15000
let tabViewId
let visitNumber = 0

export default function ReportPresence({ scope }) {
  const [count, setCount] = useState(null)

  useEffect(() => {
    tabViewId ??= globalThis.crypto?.randomUUID?.()
    if (!tabViewId) return
    const view = { view_id: tabViewId, ...(scope === 'all' ? { scope } : { project: scope }) }
    let visit = null

    function post(current, leave, signal) {
      const payload = { ...view, visit: current.number }
      return fetch('./api/v1/reports/presence', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(leave ? { ...payload, leave: true } : payload),
        keepalive: leave,
        signal,
      })
    }

    async function renew() {
      const current = visit
      if (!current) return
      let next = null
      try {
        const signal = AbortSignal.any([current.controller.signal, AbortSignal.timeout(5000)])
        const response = await post(current, false, signal)
        if (response.ok) {
          const { count } = await response.json()
          if (Number.isSafeInteger(count) && count > 0) next = count
        }
      } catch {
        // Presence is optional on a usable Reports page.
      }
      if (visit === current) setCount(next)
    }

    function leave() {
      if (!visit) return
      const current = visit
      current.controller.abort()
      visit = null
      void post(current, true).catch(() => {})
    }

    function syncVisibility() {
      if (document.visibilityState !== 'visible') {
        leave()
        setCount(null)
      } else if (!visit) {
        visit = { number: ++visitNumber, controller: new AbortController() }
        void renew()
      }
    }

    syncVisibility()
    const interval = window.setInterval(renew, renewEvery)
    document.addEventListener('visibilitychange', syncVisibility)
    window.addEventListener('pagehide', leave)
    window.addEventListener('pageshow', syncVisibility)
    return () => {
      window.clearInterval(interval)
      document.removeEventListener('visibilitychange', syncVisibility)
      window.removeEventListener('pagehide', leave)
      window.removeEventListener('pageshow', syncVisibility)
      leave()
    }
  }, [scope])

  return count == null ? null : (
    <span className="reports-viewer-count">{count} {count === 1 ? 'person' : 'people'} viewing</span>
  )
}
