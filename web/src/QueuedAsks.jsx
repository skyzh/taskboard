import React from 'react'
import { ArrowRight } from 'lucide-react'
import { useLoadedPages, useResource, queryPath } from './data.js'
import { LoadMore, ResourceStatus, TaskLink, profileName } from './shared.jsx'
import { ReportTime } from './ReportTime.jsx'
import PeerRequestHistory from './PeerRequestHistory.jsx'

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

function AskList({ label, page, items, responses = false, publicPreview = false }) {
  return (
    <>
      {page && <ResourceStatus resource={page} label={label.toLowerCase()} />}
      {items?.length > 0 && (
        <ul className="report-outcomes ask-list" aria-label={label} role="list">
          {items.map(ask => (
            <li className="ask-row" key={ask.id}>
              <AskPeople sender={responses ? ask.responder : ask.requester} recipient={ask.recipient} response={responses} />
              {ask.at && <ReportTime at={ask.at} />}
              {ask.status && <span className="ask-state">{ask.status}</span>}
              <span className="ask-ticket"><TaskLink reference={ask.linked_request_id ? ask.linked_request : ask.task_key}
                id={ask.linked_request_id ?? ask.task_id} title={ask.task_title}
                publicHref={publicPreview ? ask.public_url || null : undefined} /></span>
            </li>
          ))}
        </ul>
      )}
      {page && !responses && <LoadMore page={page} label={label.toLowerCase()} />}
    </>
  )
}

const reviewStates = { planned: 'Planned', running: 'In progress', start_unconfirmed: 'Start unconfirmed' }

export function QueuedAsksContent({ project, grouped, reviews, waits, responses, snapshot, children }) {
  const waitRows = (snapshot?.waits || waits?.data?.items)?.filter(wait => !wait.linked_request || wait.linked_request_id)
  const reviewRows = (snapshot?.reviews || reviews?.data?.items)?.filter(review => !review.linked_request).map(review => ({
    ...review, status: reviewStates[review.execution_state ?? review.state],
  }))
  const responseRows = snapshot?.responses || responses?.data?.items
  if (!children && reviewRows?.length === 0 && waitRows?.length === 0 && responseRows?.length === 0
    && reviews?.data?.next_cursor == null && waits?.data?.next_cursor == null) return null

  const headingId = grouped ? `queued-asks-${project}` : 'queued-asks-title'
  const Heading = grouped ? 'h3' : 'h2'
  const ResponseHeading = grouped ? 'h4' : 'h3'
  return (
    <section className="queued-asks" aria-labelledby={headingId}>
      <Heading id={headingId}>Queued asks</Heading>
      <AskList label="Peer reviews" page={reviews} items={reviewRows} publicPreview={Boolean(snapshot)} />
      <AskList label="Named peer waits" page={waits} items={waitRows} publicPreview={Boolean(snapshot)} />
      {responseRows?.length > 0 && <ResponseHeading>Recent responses</ResponseHeading>}
      <AskList label="Recent responses" page={responses} items={responseRows} responses publicPreview={Boolean(snapshot)} />
      {children}
    </section>
  )
}

export default function QueuedAsks({ revision, project, grouped }) {
  const reviews = useLoadedPages('/reports/queued-reviews', { project }, revision)
  const waits = useLoadedPages('/reports/queued-peer-waits', { project }, revision)
  const responses = useResource(queryPath('/reports/recent-peer-responses', { project }), revision)
  return (
    <QueuedAsksContent project={project} grouped={grouped} reviews={reviews} waits={waits} responses={responses}>
      <PeerRequestHistory key={project} project={project} revision={revision} />
    </QueuedAsksContent>
  )
}
