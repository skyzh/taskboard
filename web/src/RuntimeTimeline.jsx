import React, { useState } from 'react'
import { useTimeZone } from './timeZone.js'

const states = {
  online: 'Online',
  busy: 'Busy',
  error: 'Error',
  offline: 'Offline',
}

const activityPresence = {
  online: 'online',
  thinking: 'busy',
  working: 'busy',
  error: 'error',
  offline: 'offline',
}
const legacyPresence = { working: 'busy', idle: 'online', unavailable: 'offline' }

function presence(interval) {
  return interval.raft_activity ? activityPresence[interval.raft_activity] : legacyPresence[interval.state]
}

function ReadingTimes({ readings }) {
  const [open, setOpen] = useState(false)
  const { formatDay, formatClock } = useTimeZone()
  const time = at => <time dateTime={at}>{formatDay(at)} {formatClock(at)}</time>
  if (!readings.length) return null
  if (readings.length === 1) return <span> · Reading at {time(readings[0])}</span>
  return (
    <details onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>{readings.length} readings</summary>
      {open && <ul>{readings.map(at => <li key={at}>{time(at)}</li>)}</ul>}
    </details>
  )
}

export default function RuntimeTimeline({ timeline }) {
  const { formatTimestamp } = useTimeZone()
  const { from, to, agents } = timeline
  const start = Date.parse(from)
  const duration = Date.parse(to) - start
  const label = at => formatTimestamp(at, duration > 86_400_000)
  return (
    <section className="panel runtime-timeline" aria-labelledby="runtime-timeline-title">
      <h2 id="runtime-timeline-title">Agent working periods</h2>
      <p className="stats-note">Each minute shows its recorded reading. Rows include the agent’s overall runtime, including time spent on other projects.</p>
      <ul className="runtime-legend" role="list">
        {Object.entries(states).map(([state, name]) => <li key={state}><span className={`runtime-state ${state}`} aria-hidden="true" />{name}</li>)}
      </ul>
      <div className="runtime-axis"><time dateTime={from}>{label(from)}</time><time dateTime={to}>{label(to)}</time></div>
      {agents.length ? <ul className="runtime-agents" role="list">
        {agents.map(agent => (
          <li key={agent.id}>
            <h3>{agent.name}</h3>
            <div className="runtime-track" aria-hidden="true">
              {agent.intervals.map(interval => {
                const state = presence(interval)
                if (!state) return null
                const left = (Date.parse(interval.start_at) - start) / duration * 100
                const width = (Date.parse(interval.end_at) - Date.parse(interval.start_at)) / duration * 100
                return <span key={interval.start_at} className={`runtime-state ${state}`} style={{ left: `${left}%`, width: `${width}%` }} />
              })}
            </div>
            <details>
              <summary>Intervals for {agent.name}</summary>
              <ul>
                {agent.intervals.map(interval => (
                  <li key={interval.start_at}>
                    {states[presence(interval)] ?? (interval.readings?.length ? 'Unknown' : 'No reading')} ·{' '}
                    <time dateTime={interval.start_at}>{label(interval.start_at)}</time> to <time dateTime={interval.end_at}>{label(interval.end_at)}</time>
                    {interval.raft_status && <span> · {interval.raft_status}</span>}
                    <ReadingTimes readings={interval.readings || []} />
                  </li>
                ))}
              </ul>
            </details>
          </li>
        ))}
      </ul> : <p className="stats-note">No agents in this project.</p>}
    </section>
  )
}
