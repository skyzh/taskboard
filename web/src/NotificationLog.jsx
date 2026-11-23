import React from 'react'
import { useLoadedPages } from './data.js'
import { ReportTime } from './ReportTime.jsx'
import { LoadMore, ResourceStatus, TaskLink, Timestamp, eventHref } from './shared.jsx'
import { useTimeZone } from './timeZone.js'

const kinds = { ready: 'Ready notice', merge: 'Merge notice', wake: 'Wake' }

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

const acceptance = { pending: 'Pending', accepted: 'Accepted', rejected: 'Rejected' }
const delivery = { delivered: 'Delivered', suppressed: 'Suppressed' }
const nextResults = { offered: 'This ticket offered', different_work: 'Other work offered', no_work: 'No eligible work' }

function WakeEvidence({ item }) {
  const { formatDay, formatClock } = useTimeZone()
  const stages = [
    ['Raft acceptance', acceptance[item.acceptance] ?? 'Unknown', item.acceptance_at],
    ['Raft delivery', delivery[item.delivery] ?? 'Unknown', item.delivery_at],
    ['First signed queue check', nextResults[item.next_result] ?? 'Not observed', item.next_at],
    ['Start', item.started_at ? 'Started' : 'Not observed', item.started_at, item.start_event_id],
  ]

  return (
    <>
      <p className="notification-log-cue">{item.cue}</p>
      {item.receipt_ref && <p className="event-meta"><span title={item.receipt_ref}>Receipt {item.receipt_ref.slice(0, 12)}</span></p>}
      <dl className="notification-log-times wake-log-times">
        {stages.map(([label, outcome, at, eventId]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>
              <span>{outcome}</span>
              {at && <time dateTime={at} title={at}>{formatDay(at)} · {formatClock(at)}</time>}
              {at && eventId && <a href={eventHref(eventId)}>Start activity</a>}
            </dd>
          </div>
        ))}
      </dl>
    </>
  )
}

export function NotificationLogEntry({ item }) {
  const isWake = item.kind === 'wake'
  return (
    <li className="event-row notification-log-entry">
      <p className="event-ticket"><TaskLink id={item.task_id} reference={item.task} /></p>
      <p className="event-meta">
        <span>{kinds[item.kind]} for {item.recipient}</span>
        {!isWake && <strong>{states[item.status]}</strong>}
        {!isWake && item.status === 'pending' && <span>Observed <ReportTime at={item.status_at} /></span>}
        <a href={eventHref(item.id)}>Source activity</a>
      </p>
      {isWake ? <WakeEvidence item={item} /> : <dl className="notification-log-times">
        {times.map(([field, label]) => (
          <div key={field}>
            <dt>{label}</dt>
            <dd>{item[field] ? <Timestamp at={item[field]} /> : 'Unknown'}</dd>
          </div>
        ))}
      </dl>}
    </li>
  )
}

export default function NotificationLog({ project, state, kind, agent, projects = [], revision }) {
  const page = useLoadedPages('/notifications/log', { project, state, kind, recipient: agent }, revision)
  const items = page.data?.items ?? []
  const agentOptions = page.data?.agent_options
  const emptyMessage = page.data?.has_receipts
    ? 'No recorded notifications match these filters.'
    : agentOptions ? 'No notification receipts have been recorded for this project.'
      : 'No notification receipts have been recorded for your account in this project.'

  function chooseFilters(changes) {
    const next = { project, state, kind, agent, ...changes }
    const params = new URLSearchParams({ project: next.project })
    if (next.state !== 'all') params.set('status', next.state)
    if (next.kind) params.set('kind', next.kind)
    if (next.agent && next.project === project) params.set('agent', next.agent)
    window.location.hash = `notifications-log?${params}`
  }

  return (
    <section className="feed-section" aria-labelledby="notification-log-title">
      <h2 id="notification-log-title">Project notification log</h2>
      <p>Recorded evidence for ticket-linked Ready notices, merge notices, and wakes.</p>
      <div className="filters log-filters">
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
        {agentOptions && (
          <label>Agent
            <select value={agent} onChange={event => chooseFilters({ agent: event.target.value })}>
              <option value="">All agents</option>
              {agentOptions.map(option => <option key={option.id} value={option.id}>{option.name}</option>)}
            </select>
          </label>
        )}
      </div>
      <ResourceStatus resource={page} label="project notifications" />
      {agent && [403, 404].includes(page.error?.status) && (
        <button type="button" onClick={() => chooseFilters({ agent: '' })}>Clear agent filter</button>
      )}
      {page.data && items.length === 0 && <p className="section-message">{emptyMessage}</p>}
      {items.length > 0 && (
        <ol className="event-list" aria-label="Project notifications" role="list">
          {items.map(item => <NotificationLogEntry key={`${item.kind}:${item.id}`} item={item} />)}
        </ol>
      )}
      <LoadMore page={page} label="project notifications" />
    </section>
  )
}
