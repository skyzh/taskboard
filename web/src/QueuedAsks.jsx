import React from 'react'
import { useLoadedPages } from './data.js'
import { LoadMore, ResourceStatus, TaskLink, profileName } from './shared.jsx'
import { Markdown } from './Markdown.jsx'

function AskList({ label, page, items, empty }) {
  return (
    <>
      <ResourceStatus resource={page} label={label.toLowerCase()} />
      {items?.length === 0 && empty && <p className="section-message">{empty}</p>}
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

function PersonalQueuedAsks({ revision, project }) {
  const decisions = useLoadedPages('/notifications/decisions', { project }, revision)
  const requests = useLoadedPages('/notifications/requests', { project }, revision)
  const decisionRows = decisions.data?.items.map(task => ({
    id: task.id, task_id: task.id, task_key: task.key, task_title: task.title,
    type: 'Decision request', status: 'Awaiting your decision', note: task.note,
  }))
  const requestRows = requests.data?.items.map(request => ({
    ...request, type: 'Attention request', status: 'Awaiting your attention', note: request.reason,
  }))
  return (
    <>
      <AskList label="Decision requests" page={decisions} items={decisionRows} />
      <AskList label="Attention requests" page={requests} items={requestRows} />
    </>
  )
}

const peerReviewStates = { planned: 'Planned', running: 'In progress', start_unconfirmed: 'Start unconfirmed' }

export default function QueuedAsks({ revision, viewer, project, grouped }) {
  const reviews = useLoadedPages('/reports/queued-reviews', { project }, revision)
  const reviewRows = reviews.data?.items.map(review => ({
    ...review, type: 'Peer review', status: peerReviewStates[review.execution_state ?? review.state],
  }))
  const headingId = grouped ? `queued-asks-${project}` : 'queued-asks-title'
  const Heading = grouped ? 'h3' : 'h2'
  return (
    <section className="queued-asks" aria-labelledby={headingId}>
      <Heading id={headingId}>Queued asks</Heading>
      <AskList label="Peer reviews" page={reviews} items={reviewRows} empty="No pending peer reviews." />
      {viewer && <PersonalQueuedAsks key={viewer.id} revision={revision} project={project} />}
    </section>
  )
}
