import React, { useState } from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { queryPath, useResource } from './data.js'
import { projectHref } from './projectScope.js'
import { ResourceStatus, Timestamp, eventHref } from './shared.jsx'

function WatcherRow({ row, project }) {
  const watcher = row.watcher
  const state = watcher?.state === 'running' ? 'Running' : 'Exited'
  const agent = new URLSearchParams({ dri: row.agent, project })
  return (
    <li className="event-row">
      <p className="event-ticket"><BoardLink href={`#${agent}`}>{row.name}</BoardLink></p>
      {watcher ? <>
        <p className="event-meta">
          <strong>{watcher.stale ? `Stale · last reported ${state.toLowerCase()}` : state}</strong>
          <span>Last report <Timestamp at={watcher.reported_at} /></span>
        </p>
        <dl className="notification-log-times">
          <div><dt>Revision</dt><dd>{watcher.revision ? <code title={watcher.revision}>{watcher.revision.slice(0, 12)}</code> : 'Unavailable'}</dd></div>
          <div><dt>Ready</dt><dd>{watcher.ready ? 'On' : 'Off'}</dd></div>
          <div><dt>Goal</dt><dd>{watcher.goals ? 'On' : 'Off'}</dd></div>
          <div><dt>Stalled work</dt><dd>{watcher.stalled ? 'On' : 'Off'}</dd></div>
        </dl>
      </> : <p className="event-meta">Watcher report unavailable</p>}
      <p className="event-meta">Last verified pickup{' '}
        {row.pickup ? <BoardLink href={projectHref(eventHref(row.pickup.event_id), project)}>
          {row.pickup.ticket} · <Timestamp at={row.pickup.at} />
        </BoardLink> : 'Unknown'}
      </p>
    </li>
  )
}

export default function AgentManagement({ project, revision }) {
  const [refresh, setRefresh] = useState(0)
  const health = useResource(project ? queryPath('/reports/watchers', { project }) : null, `${revision}:${refresh}`)
  const rows = health.data?.items || []
  return (
    <section className="feed-section" aria-labelledby="agent-management">
      <h1 id="agent-management">Agent management</h1>
      <p>Watcher reports describe the managed watcher process. Runtime activity and ticket status are separate.</p>
      <button type="button" onClick={() => setRefresh(value => value + 1)} disabled={!project || health.pending}>Refresh watcher reports</button>
      <ResourceStatus resource={health} label="watcher reports" />
      {health.data && rows.length === 0 && <p className="section-message">No agents in this project.</p>}
      {rows.length > 0 && (
        <ul className="event-list" aria-label="Agent watchers">
          {rows.map(row => <WatcherRow row={row} project={project} key={row.agent} />)}
        </ul>
      )}
    </section>
  )
}
