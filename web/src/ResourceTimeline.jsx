import React, { useId } from 'react'
import { useTimeZone } from './timeZone.js'

export function claimIntervals(items, current, asOf) {
  const now = Date.parse(asOf)
  if (!Number.isFinite(now)) return []
  const latest = new Map()
  for (const event of items) {
    const id = event.claim?.claim_id
    if (id && (!latest.has(id) || latest.get(id).event_id < event.event_id)) latest.set(id, event)
  }
  if (current && !latest.has(current.claim_id)) {
    latest.set(current.claim_id, { claim: current })
  }
  return [...latest.values()].flatMap(({ claim, action, at }) => {
    const start = Date.parse(claim.claimed_at)
    const expiry = Date.parse(claim.expires_at)
    const closed = action === 'release' || action === 'expire'
    const active = !closed && current?.claim_id === claim.claim_id && Date.parse(current.expires_at) > now
    const end = closed ? Math.min(Date.parse(at), expiry, now) : active ? now : NaN
    return Number.isFinite(start) && Number.isFinite(end) && start <= end
      ? [{ id: claim.claim_id, holder: claim.holder, start, end, active }] : []
  }).sort((a, b) => a.start - b.start)
}

function holderColor(id) {
  let hash = 2166136261
  for (const char of id) hash = Math.imul(hash ^ char.codePointAt(0), 16777619)
  return `hsl(${(hash >>> 0) % 360} 64% 46%)`
}

export default function ResourceTimeline({ items, current, asOf }) {
  const heading = useId()
  const { formatTimestamp } = useTimeZone()
  const intervals = claimIntervals(items, current, asOf)
  if (!intervals.length) return null
  const start = intervals[0].start
  const end = Date.parse(asOf)
  const range = Math.max(1000, end - start)
  const holders = new Map(intervals.map(interval => [interval.holder?.id ?? '', interval.holder?.name || 'Unavailable assignee']))
  const time = instant => new Date(instant).toISOString()
  const label = instant => formatTimestamp(time(instant), range > 86_400_000)
  return (
    <section className="resource-timeline" aria-labelledby={heading}>
      <h3 id={heading}>Claim timeline</h3>
      <ul className="resource-timeline-legend" aria-label="Claim holders">
        {[...holders].map(([id, name]) => <li key={id}><span style={{ backgroundColor: holderColor(id) }} aria-hidden="true" />{name}</li>)}
      </ul>
      <div className="resource-timeline-track" aria-label="Recorded claim intervals">
        {intervals.map(({ id, holder, start: left, end: right, active }) => {
          const caption = `${holder?.name || 'Unavailable assignee'} · ${label(left)} to ${label(right)}${active ? ' · Current claim' : ''}`
          return <span key={id} role="img" aria-label={caption} title={caption} data-active={active || undefined}
            style={{ left: `${(left - start) / range * 100}%`, width: `${(right - left) / range * 100}%`,
              backgroundColor: holderColor(holder?.id ?? '') }} />
        })}
      </div>
      <div className="resource-timeline-axis">
        <time dateTime={time(start)}>{label(start)}</time><time dateTime={time(end)}>{label(end)}</time>
      </div>
      {intervals.some(interval => interval.active) && <p className="schedule-muted">Current claim shown as of the last refresh.</p>}
    </section>
  )
}
