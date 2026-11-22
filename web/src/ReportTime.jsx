import React, { createContext, useContext, useEffect, useState } from 'react'
import { useTimeZone } from './timeZone.js'

const ReportClock = createContext(null)
const relativeTime = new Intl.RelativeTimeFormat('en', { style: 'narrow' })

export function relativeAge(at, now) {
  const minutes = Math.max(0, Math.floor((now - Date.parse(at)) / 60_000))
  if (!Number.isFinite(minutes)) return at
  if (minutes === 0) return 'just now'
  if (minutes < 60) return relativeTime.format(-minutes, 'minute')
  if (minutes < 1440) return relativeTime.format(-Math.floor(minutes / 60), 'hour')
  return relativeTime.format(-Math.floor(minutes / 1440), 'day')
}

export function watchReportTime(onTick) {
  let timer
  function sync() {
    window.clearInterval(timer)
    if (document.visibilityState === 'hidden') return
    onTick(Date.now())
    timer = window.setInterval(() => onTick(Date.now()), 15_000)
  }
  document.addEventListener('visibilitychange', sync)
  sync()
  return () => {
    document.removeEventListener('visibilitychange', sync)
    window.clearInterval(timer)
  }
}

export function ReportTimeProvider({ children }) {
  const [now, setNow] = useState(Date.now)
  useEffect(() => watchReportTime(setNow), [])
  return <ReportClock.Provider value={now}>{children}</ReportClock.Provider>
}

export function ReportTime({ at }) {
  const now = useContext(ReportClock)
  const { formatTimestamp } = useTimeZone()
  const age = relativeAge(at, now ?? Date.now())
  return <time dateTime={at} title={age === at ? at : formatTimestamp(at)}>{age}</time>
}
