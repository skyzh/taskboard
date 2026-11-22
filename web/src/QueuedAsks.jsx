import React from 'react'
import { useLoadedPages } from './data.js'
import { LoadMore, ResourceStatus, TaskLink, profileName } from './shared.jsx'
import { Markdown } from './Markdown.jsx'

function AskList({ label, page, items }) {
  return (
    <>
      <ResourceStatus resource={page} label={label.toLowerCase()} />
      {items?.length > 0 && (
        <ul className="event-list" aria-label={label} role="list">
          {items.map(ask => (
            <li className="event-row" key={ask.id}>
              <p><TaskLink reference={ask.task_key} id={ask.task_id} title={ask.task_title} /></p>
              <p className="event-meta">
                <span>{ask.type}</span>
                {ask.recipient && <span>For {profileName(ask.recipient)}</span>}
                <span>{ask.status}</span>
              </p>
              {ask.note && <Markdown text={ask.note} />}
            </li>
          ))}
        </ul>
      )}
      <LoadMore page={page} label={label.toLowerCase()} />
    </>
  )
}

const peerReviewStates = { planned: 'Planned', running: 'In progress', start_unconfirmed: 'Start unconfirmed' }

export default function QueuedAsks({ revision, project, grouped }) {
  const reviews = useLoadedPages('/reports/queued-reviews', { project }, revision)
  const waits = useLoadedPages('/reports/queued-peer-waits', { project }, revision)
  const waitRows = waits.data?.items.filter(wait => !wait.linked_request).map(wait => ({
    ...wait, type: 'Named peer wait', status: 'Waiting for reply',
  }))
  const reviewRows = reviews.data?.items.filter(review => !review.linked_request).map(review => ({
    ...review, type: 'Peer review', status: peerReviewStates[review.execution_state ?? review.state],
  }))
  if (reviewRows?.length === 0 && waitRows?.length === 0 && reviews.data.next_cursor == null && waits.data.next_cursor == null) return null
  const headingId = grouped ? `queued-asks-${project}` : 'queued-asks-title'
  const Heading = grouped ? 'h3' : 'h2'
  return (
    <section className="queued-asks" aria-labelledby={headingId}>
      <Heading id={headingId}>Queued asks</Heading>
      <AskList label="Peer reviews" page={reviews} items={reviewRows} />
      <AskList label="Named peer waits" page={waits} items={waitRows} />
    </section>
  )
}
