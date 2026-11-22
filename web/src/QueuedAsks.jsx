import React from 'react'
import { ArrowRight } from 'lucide-react'
import { useLoadedPages, useResource, queryPath } from './data.js'
import { LoadMore, ResourceStatus, TaskLink, profileName } from './shared.jsx'
import { ReportTime } from './ReportTime.jsx'

function AskPeople({ sender, recipient, response }) {
  const target = profileName(recipient)
  if (!sender) return <span>For {target}</span>
  const origin = profileName(sender)
  return (
    <span className="ask-people">
      <span className="relative-time-accessible">
        {response ? `${origin} responded to ${target}` : `${origin} requested ${target}`}
      </span>
      <span aria-hidden="true">{origin} <ArrowRight size={13} focusable="false" /> {target}</span>
    </span>
  )
}

function AskList({ label, page, items, responses = false }) {
  return (
    <>
      <ResourceStatus resource={page} label={label.toLowerCase()} />
      {items?.length > 0 && (
        <ul className="report-outcomes ask-list" aria-label={label} role="list">
          {items.map(ask => (
            <li className="ask-row" key={ask.id}>
              <AskPeople sender={responses ? ask.responder : ask.requester} recipient={ask.recipient} response={responses} />
              {ask.at && <ReportTime at={ask.at} />}
              {ask.status && <span className="ask-state">{ask.status}</span>}
              <span className="ask-ticket"><TaskLink reference={ask.task_key} id={ask.task_id} title={ask.task_title} /></span>
            </li>
          ))}
        </ul>
      )}
      {!responses && <LoadMore page={page} label={label.toLowerCase()} />}
    </>
  )
}

const reviewStates = { planned: 'Planned', running: 'In progress', start_unconfirmed: 'Start unconfirmed' }

export default function QueuedAsks({ revision, project, grouped }) {
  const reviews = useLoadedPages('/reports/queued-reviews', { project }, revision)
  const waits = useLoadedPages('/reports/queued-peer-waits', { project }, revision)
  const responses = useResource(queryPath('/reports/recent-peer-responses', { project }), revision)
  const waitRows = waits.data?.items.filter(wait => !wait.linked_request)
  const reviewRows = reviews.data?.items.filter(review => !review.linked_request).map(review => ({
    ...review, status: reviewStates[review.execution_state ?? review.state],
  }))
  const responseRows = responses.data?.items
  if (reviewRows?.length === 0 && waitRows?.length === 0 && responseRows?.length === 0
    && reviews.data.next_cursor == null && waits.data.next_cursor == null) return null

  const headingId = grouped ? `queued-asks-${project}` : 'queued-asks-title'
  const Heading = grouped ? 'h3' : 'h2'
  const ResponseHeading = grouped ? 'h4' : 'h3'
  return (
    <section className="queued-asks" aria-labelledby={headingId}>
      <Heading id={headingId}>Queued asks</Heading>
      <AskList label="Peer reviews" page={reviews} items={reviewRows} />
      <AskList label="Named peer waits" page={waits} items={waitRows} />
      {responseRows?.length > 0 && <ResponseHeading>Recent responses</ResponseHeading>}
      <AskList label="Recent responses" page={responses} items={responseRows} responses />
    </section>
  )
}
