import React from 'react'
import { useTimeZone } from './timeZone.js'

const states = {
  working: ['Working', 'working'],
  idle: ['Idle', 'idle'],
  unavailable: ['Unavailable', 'unavailable'],
  unknown: ['Unknown', 'unknown'],
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
      <p className="stats-note">Each row shows the agent’s overall runtime, including time spent on other projects.</p>
      <ul className="runtime-legend" role="list">
        {Object.values(states).map(([name, style]) => <li key={style}><span className={`runtime-state ${style}`} aria-hidden="true" />{name}</li>)}
      </ul>
      <div className="runtime-axis"><time dateTime={from}>{label(from)}</time><time dateTime={to}>{label(to)}</time></div>
      {agents.length ? <ul className="runtime-agents" role="list">
        {agents.map(agent => (
          <li key={agent.id}>
            <h3>{agent.name}</h3>
            <div className="runtime-track runtime-state unknown" aria-hidden="true">
              {agent.intervals.map(interval => {
                const [, state] = states[interval.state] ?? states.unknown
                const left = (Date.parse(interval.start_at) - start) / duration * 100
                const width = (Date.parse(interval.end_at) - Date.parse(interval.start_at)) / duration * 100
                return <span key={interval.start_at} className={`runtime-state ${state}`} style={{ left: `${left}%`, width: `${width}%` }} />
              })}
            </div>
            <details>
              <summary>Intervals for {agent.name}</summary>
              <ul>
                {agent.intervals.map(({ start_at: from, end_at: to, state }) => (
                  <li key={from}>{states[state]?.[0] ?? 'Unknown'} · <time dateTime={from}>{label(from)}</time> to <time dateTime={to}>{label(to)}</time></li>
                ))}
              </ul>
            </details>
          </li>
        ))}
      </ul> : <p className="stats-note">No agents in this project.</p>}
    </section>
  )
}
