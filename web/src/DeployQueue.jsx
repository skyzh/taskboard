import React, { useState } from 'react'
import { useLoadedPages } from './data.js'
import { LoadMore, ResourceStatus, TaskLink, Timestamp } from './shared.jsx'

const labels = {
  queued: 'Queued',
  running: 'Deploying',
  released: 'Released',
  rejected: 'Rejected',
  rolled_back: 'Rolled back',
  rollback_failed: 'Rollback failed',
}

export function ReleaseFields({ task }) {
  if (typeof task.queued_for_release !== 'boolean') return null
  return (
    <dl className="task-release-fields">
      <div><dt>Queued for release</dt><dd>{task.queued_for_release ? 'Yes' : 'No'}</dd></div>
      <div><dt>Release number</dt><dd>{task.release_number ?? '—'}</dd></div>
    </dl>
  )
}

export function ReleaseBatch({ batch }) {
  const status = labels[batch.status] || batch.status
  const numbered = batch.release_number != null
  const entries = batch.status === 'released' && numbered
    ? batch.changelog?.entries
    : null
  return (
    <li className="event-row">
      <p><strong>{numbered ? `Release #${batch.release_number}` : status}</strong></p>
      <p className="event-meta">
        {numbered && <span>{status}</span>}
        <Timestamp at={batch.at} />
      </p>
      <ul className="context-list">
        {entries?.length ? entries.map(entry => (
          <li key={entry.merge_receipt_id}>
            <TaskLink id={entry.ticket.replace(/^T-/, '')} reference={entry.ticket} title={entry.summary} />
            {' · '}<a href={entry.merge_receipt_url} title={entry.merge_commit}
              aria-label={`Merge receipt for ${entry.ticket}`}>Merge receipt</a>
          </li>
        )) : batch.tickets.map(ticket => (
          <li key={ticket.id}><TaskLink id={ticket.id} reference={ticket.key} title={ticket.title} /></li>
        ))}
      </ul>
      <details>
        <summary>Release details</summary>
        <p>Release ID <code>{batch.release_id}</code></p>
        {batch.receipt_url && <p><a href={batch.receipt_url}>Receipt</a></p>}
      </details>
    </li>
  )
}

function QueueEntries({ project, revision }) {
  const page = useLoadedPages('/deploy-queue', { project }, revision)
  return (
    <>
      <ResourceStatus resource={page} label="the deploy queue" />
      {page.data?.items.length === 0 && <p className="section-message">No release receipts yet.</p>}
      {page.data?.items.length > 0 && (
        <ol className="event-list" aria-label="Deploy queue">
          {page.data.items.map(batch => <ReleaseBatch key={batch.release_id} batch={batch} />)}
        </ol>
      )}
      <LoadMore page={page} label="deploy queue entries" />
    </>
  )
}

export function DeployQueue({ project, revision }) {
  const [open, setOpen] = useState(false)
  return (
    <details onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>Deploy queue</summary>
      {open && <QueueEntries project={project} revision={revision} />}
    </details>
  )
}
