import React, { createContext, useContext, useEffect, useId, useState } from 'react'
import { useTimeZone } from './timeZone.js'
import { DemoControl, DemoControlDescription } from './DemoControl.jsx'

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

export function ReportTimeProvider({ children, capturedAt }) {
  const [now, setNow] = useState(Date.now)
  useEffect(() => capturedAt ? undefined : watchReportTime(setNow), [capturedAt])
  return <ReportClock.Provider value={capturedAt ? Date.parse(capturedAt) : now}>{children}</ReportClock.Provider>
}

export function useReportDay() {
  const now = useContext(ReportClock)
  const { formatDay } = useTimeZone()
  return formatDay(now ?? Date.now())
}

function ExactTimeDisclosure({ at, age, exact }) {
  const id = useId()
  const [expanded, setExpanded] = useState(false)
  const demo = useContext(DemoControlDescription)
  return (
    <span className="relative-time">
      <DemoControl as="button" type="button" className="relative-time-toggle" aria-expanded={expanded} aria-controls={id}
        aria-label={`${age}, ${exact}`} onClick={() => setExpanded(value => !value)}>
        <time dateTime={at} title={demo ? undefined : exact}>{age}</time>
      </DemoControl>
      <time id={id} className="relative-time-detail" dateTime={at} hidden={!expanded}>{exact}</time>
    </span>
  )
}

export function ReportTime({ at, linked = false }) {
  const now = useContext(ReportClock)
  const { formatTimestamp } = useTimeZone()
  const age = relativeAge(at, now ?? Date.now())
  if (age === at) return <time dateTime={at}>{at}</time>
  const exact = formatTimestamp(at)
  if (linked) {
    return <time dateTime={at} title={exact}>{age}<span className="relative-time-accessible"> ({exact})</span></time>
  }
  return <ExactTimeDisclosure at={at} age={age} exact={exact} />
}
