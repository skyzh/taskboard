import React from 'react'
import { useLoadedPages } from './data.js'
import { LoadMore, ResourceStatus, TaskLink, Timestamp, eventHref } from './shared.jsx'

const kinds = { ready: 'Ready notice', merge: 'Merge notice' }

const states = {
  pending: 'Pending',
  send_confirmed: 'Send confirmed',
  delivered: 'Delivered',
  read: 'Read',
  acknowledged: 'Acknowledged',
  failed: 'Failed',
  unknown: 'Unknown',
}

const times = [
  ['requested_at', 'Requested'],
  ['send_confirmed_at', 'Send confirmation recorded'],
  ['delivered_at', 'Delivered'],
  ['read_at', 'Read'],
  ['acknowledged_at', 'Acknowledged'],
  ['failed_at', 'Failed'],
]

function Entry({ item }) {
  return (
    <li className="event-row notification-log-entry">
      <p className="event-ticket"><TaskLink id={item.task_id} reference={item.task} /></p>
      <p className="event-meta">
        <span>{kinds[item.kind]} for {item.recipient}</span>
        <strong>{states[item.status]}</strong>
        {item.status === 'pending' && <span>Observed <Timestamp at={item.status_at} /></span>}
        <a href={eventHref(item.id)}>Source activity</a>
      </p>
      <dl className="notification-log-times">
        {times.map(([field, label]) => (
          <div key={field}>
            <dt>{label}</dt>
            <dd>{item[field] ? <Timestamp at={item[field]} /> : 'Unknown'}</dd>
          </div>
        ))}
      </dl>
    </li>
  )
}

export default function NotificationLog({ project, state, kind, projects = [], revision }) {
  const page = useLoadedPages('/notifications/log', { project, state, kind }, revision)
  const items = page.data?.items ?? []

  function chooseFilters(changes) {
    const next = { project, state, kind, ...changes }
    const params = new URLSearchParams({ project: next.project })
    if (next.state !== 'all') params.set('status', next.state)
    if (next.kind) params.set('kind', next.kind)
    window.location.hash = `notifications-log?${params}`
  }

  return (
    <section className="feed-section" aria-labelledby="notification-log-title">
      <h2 id="notification-log-title">Project notification log</h2>
      <p>Delivery, read, and acknowledgment times appear when the source records them.</p>
      <div className="log-filters">
        {projects.length > 1 && (
          <label>Project
            <select value={project} onChange={event => chooseFilters({ project: event.target.value })}>
              {projects.map(entry => <option key={entry.prefix} value={entry.prefix}>{entry.title}</option>)}
            </select>
          </label>
        )}
        <label>Status
          <select value={state} onChange={event => chooseFilters({ state: event.target.value })}>
            <option value="all">All records</option>
            <option value="pending">Pending</option>
            <option value="history">History</option>
          </select>
        </label>
        <label>Kind
          <select value={kind} onChange={event => chooseFilters({ kind: event.target.value })}>
            <option value="">All notices</option>
            {Object.entries(kinds).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
      </div>
      <ResourceStatus resource={page} label="project notifications" />
      {page.data && items.length === 0 && <p className="section-message">No recorded notifications match these filters.</p>}
      {items.length > 0 && (
        <ol className="event-list" aria-label="Project notifications" role="list">
          {items.map(item => <Entry key={item.id} item={item} />)}
        </ol>
      )}
      <LoadMore page={page} label="project notifications" />
    </section>
  )
}
