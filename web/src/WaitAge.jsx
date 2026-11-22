import React from 'react'
import { useTimeZone } from './timeZone.js'

const zonedTimestamp = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/

function duration(since) {
  if (typeof since !== 'string' || !zonedTimestamp.test(since)) return null
  const minutes = Math.floor((Date.now() - Date.parse(since)) / 60_000)
  if (!Number.isFinite(minutes) || minutes < 0) return null
  for (const [unit, size] of [['day', 1440], ['hour', 60], ['minute', 1]]) {
    const count = Math.floor(minutes / size)
    if (count) return `${count} ${unit}${count === 1 ? '' : 's'}`
  }
  return 'under a minute'
}

export function WaitAge({ since, label, href }) {
  const { formatTimestamp } = useTimeZone()
  const elapsed = duration(since)
  if (elapsed === null) return null
  const timestamp = formatTimestamp(since)
  const age = <time className="wait-age" dateTime={since} title={timestamp}
    aria-label={`${label} for ${elapsed} (since ${timestamp})`}>{label} for {elapsed}</time>
  return href ? <a href={href}>{age}</a> : age
}

export function TaskWaitAge({ task, href }) {
  if (task.state === 'waiting') {
    return <WaitAge since={task.waiting_since} label={task.blockers ? 'Blocked' : 'Waiting'} href={href} />
  }
  if (task.state === 'human_review' && ['assign-human', 'redirect-decision'].includes(task.action)) {
    return <WaitAge since={task.at} label="Unanswered" href={href} />
  }
  return null
}
