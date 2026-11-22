import React from 'react'
import { ArrowRight, Check, ChevronRight, CircleX, Clock, GitPullRequest, MessageSquare, Play, Reply } from 'lucide-react'
import { useLoadedPages } from './data.js'
import { AssigneeAvatar, eventHref, LoadMore, peerRequestLabels, profileName, ResourceStatus, TaskLink } from './shared.jsx'
import { ReportTime } from './ReportTime.jsx'

const phases = {
  asked: ['Asked', MessageSquare],
  waiting: ['Waiting', Clock],
  in_progress: ['In progress', Play],
  start_unconfirmed: ['Start unconfirmed', Clock],
  acknowledgment: ['Ready to acknowledge', Reply],
  done: ['Done', Check, true],
  canceled: ['Canceled', CircleX, true],
  superseded: [peerRequestLabels.superseded, CircleX, true],
  closed_without_response: [peerRequestLabels.closed_without_response, CircleX, true],
  closed: [peerRequestLabels.closed, CircleX, true],
}

function Person({ name, profile }) {
  const label = profileName(name, profile)
  return <span className="request-person"><AssigneeAvatar name={label} url={profile?.avatar_url} />{label}</span>
}

function RequestRow({ item, viewer, publicPreview }) {
  const [status, Icon, terminal] = phases[item.phase] || ['Status unavailable', MessageSquare]
  const summary = item.request_title || item.source_title || 'Request'
  const responseHref = item.response ? eventHref(item.response.id) : null
  const canReviewResponse = item.phase === 'acknowledgment' && item.next_owner && viewer?.id === item.next_owner
  const owner = item.next_owner || item.recipient
  const ownerProfile = item.next_owner ? item.next_owner_profile : item.recipient_profile
  const ownerName = profileName(owner, ownerProfile)
  return (
    <li className={terminal ? 'request-row request-terminal' : 'request-row'}>
      <details className="request-disclosure">
        <summary className="request-summary">
          <Icon size={14} aria-hidden="true" />
          <span className="request-summary-text" title={summary}>{summary}</span>
          <span className="relative-time-accessible">{status}</span>
          {owner && <span className="request-summary-avatar" title={ownerName}>
            <AssigneeAvatar name={ownerName} url={ownerProfile?.avatar_url} />
            <span className="relative-time-accessible">{item.next_owner ? `Next for ${ownerName}` : ownerName}</span>
          </span>}
          <ChevronRight size={14} aria-hidden="true" className="request-chevron" />
        </summary>
        <div className="request-details">
          <div className="request-row-line">
            <span className="request-status">
              {item.review && <span className="request-review"><GitPullRequest size={14} aria-hidden="true" />Review</span>}
              {status}
            </span>
            {item.at && <ReportTime at={item.at} />}
          </div>
          <div className="request-row-line">
            {item.requester && <>
              <Person name={item.requester} profile={item.requester_profile} />
              <ArrowRight size={13} aria-hidden="true" />
              <span className="relative-time-accessible">asked</span>
            </>}
            <Person name={item.recipient} profile={item.recipient_profile} />
          </div>
          {item.next_owner && (
            <span className="request-owner">Next for {profileName(item.next_owner, item.next_owner_profile)}</span>
          )}
          <div className="request-row-line">
            {item.request_key && <TaskLink reference={item.request_key} title="Request" />}
            <TaskLink id={item.source_id} reference={item.source_key} title={item.source_title}
              publicHref={publicPreview ? item.public_url || null : undefined} />
          </div>
          {(responseHref || item.resolution) && (
            <div className="request-row-line">
              {responseHref && <a href={responseHref}>Response</a>}
              {item.resolution && <a href={eventHref(item.resolution.id)}>{status} record</a>}
            </div>
          )}
        </div>
      </details>
      {canReviewResponse && responseHref && <a className="request-action" href={responseHref}>Review response</a>}
    </li>
  )
}

function snapshotRows({ reviews, waits, responses }) {
  const reviewPhases = { planned: 'asked', running: 'in_progress', start_unconfirmed: 'start_unconfirmed' }
  const rows = [
    ...reviews.filter(item => !item.linked_request).map(item => ({
      ...item, review: true, phase: reviewPhases[item.execution_state ?? item.state] || 'asked',
    })),
    ...waits.map(item => ({ ...item, phase: 'waiting' })),
    ...responses.map(item => ({ ...item, phase: 'done', requester: item.recipient, recipient: item.responder })),
  ]
  return rows.map(item => ({
    id: `${item.phase}:${item.id}`,
    at: item.at,
    phase: item.phase,
    review: item.review,
    requester: item.requester,
    recipient: item.recipient,
    next_owner: item.phase === 'done' ? null : item.recipient,
    source_id: item.task_id,
    source_key: item.task_key,
    source_title: item.task_title,
    public_url: item.public_url,
  })).sort((left, right) => (right.at || '').localeCompare(left.at || ''))
}

export function QueuedAsksContent({ project, grouped, page, snapshot, viewer }) {
  const items = snapshot ? snapshotRows(snapshot) : page?.data?.items
  if (items?.length === 0 && (snapshot || (!page.pending && !page.error))) return null

  const headingId = grouped ? `request-log-${project}` : 'request-log-title'
  const Heading = grouped ? 'h3' : 'h2'
  return (
    <section className="queued-asks" aria-labelledby={headingId}>
      <Heading id={headingId}>Request log</Heading>
      {page && <ResourceStatus resource={page} label="request log" />}
      {items?.length > 0 && (
        <ul className="report-outcomes request-log" aria-label="Requests" role="list">
          {items.map(item => <RequestRow key={item.id} item={item} viewer={viewer} publicPreview={Boolean(snapshot)} />)}
        </ul>
      )}
      {page && <LoadMore page={page} label="requests" />}
    </section>
  )
}

export default function QueuedAsks({ revision, project, grouped, viewer }) {
  const page = useLoadedPages('/reports/request-log', { project }, revision, { limit: 10 })
  return <QueuedAsksContent project={project} grouped={grouped} page={page} viewer={viewer} />
}
