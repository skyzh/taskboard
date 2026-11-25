import React, { useState } from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { ArrowRight, Check, CircleX, Clock, MessageSquare, Play, Reply } from 'lucide-react'
import { useLoadedPages } from './data.js'
import { AssigneeAvatar, eventHref, LoadMore, peerRequestLabels, profileName, ResourceStatus, taskHref, TaskLink } from './shared.jsx'
import { ReportTime } from './ReportTime.jsx'
import SegmentedControl from './SegmentedControl.jsx'

const displayModes = [
  { value: 'compact', label: 'Compact' },
  { value: 'verbose', label: 'Verbose' },
]

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

function RequestRow({ item, viewer, publicPreview, verbose, projectLabel }) {
  const [status, Icon, terminal] = phases[item.phase] || ['Status unavailable', MessageSquare]
  const responseHref = item.response ? eventHref(item.response.id) : null
  const canReviewResponse = item.phase === 'acknowledgment' && item.next_owner && viewer?.id === item.next_owner
  const rowClass = terminal ? 'request-row request-terminal' : 'request-row'
  if (verbose) {
    const pulls = item.pull_urls || []
    const canceled = ['canceled', 'closed_without_response'].includes(item.phase)
    const statusLabel = canceled && !item.response ? 'Canceled without a response' : status
    return (
      <li className={rowClass}>
        <div className="request-details">
          <div className="request-state">
            <span className="request-status"><Icon size={14} aria-hidden="true" />{statusLabel}</span>
            {projectLabel && <span className="report-project-label">{projectLabel}</span>}
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
          {item.request_key && (
            <div className="request-row-line">
              <BoardLink href={taskHref(item.request_key, item.request_key)}>
                <span className="ticket-id">{item.request_key}</span>{item.request_title && ` ${item.request_title}`}
              </BoardLink>
            </div>
          )}
          <div className="request-row-line">
            <TaskLink id={item.source_id} reference={item.source_key} title={item.source_title}
              publicHref={publicPreview ? item.public_url || null : undefined} />
          </div>
          {item.next_owner && (
            <span className="request-owner">Next for {profileName(item.next_owner, item.next_owner_profile)}</span>
          )}
          {(pulls.length > 0 || responseHref || item.resolution) && (
            <div className="request-row-line">
              {pulls.map(url => (
                <BoardLink key={url} href={url}>
                  {pulls.length === 1 ? 'Pull request' : url.replace('https://github.com/', '').replace('/pull/', '#')}
                </BoardLink>
              ))}
              {responseHref && <BoardLink href={responseHref}>{canReviewResponse ? 'Review response' : 'Response'}</BoardLink>}
              {item.resolution && <BoardLink href={eventHref(item.resolution.id)}>{canceled ? 'Cancellation record' : `${status} record`}</BoardLink>}
            </div>
          )}
        </div>
      </li>
    )
  }

  const summary = item.request_title || item.source_title
  const label = summary || (item.request_key ? <span className="ticket-id">{item.request_key}</span> : 'Request')
  const requestHref = publicPreview ? item.public_url
    : taskHref(item.request_key ?? item.source_id, item.request_key ?? item.source_key)
  const owner = item.next_owner || item.recipient
  const ownerProfile = item.next_owner ? item.next_owner_profile : item.recipient_profile
  const ownerName = profileName(owner, ownerProfile)
  return (
    <li className={rowClass}>
      <div className="request-summary">
        <Icon size={14} aria-hidden="true" />
        <div className="request-summary-copy">
          {requestHref
            ? <BoardLink className="request-summary-text" href={requestHref} title={summary}>{label}</BoardLink>
            : <span className="request-summary-text">{label}</span>}
          {projectLabel && <span className="report-project-label">{projectLabel}</span>}
        </div>
        <span className="relative-time-accessible">{status}</span>
        {owner && <span className="request-summary-avatar" title={ownerName}>
          <AssigneeAvatar name={ownerName} url={ownerProfile?.avatar_url} />
          <span className="relative-time-accessible">{item.next_owner ? `Next for ${ownerName}` : ownerName}</span>
        </span>}
      </div>
      {canReviewResponse && responseHref && <BoardLink className="request-action" href={responseHref}>Review response</BoardLink>}
    </li>
  )
}

function snapshotRows({ reviews, waits, responses }, profiles) {
  const reviewPhases = { planned: 'asked', running: 'in_progress', start_unconfirmed: 'start_unconfirmed' }
  const rows = [
    ...reviews.filter(item => !item.linked_request).map(item => ({
      ...item, review: true, phase: reviewPhases[item.execution_state ?? item.state] || 'asked',
    })),
    ...waits.map(item => ({ ...item, phase: 'waiting' })),
    ...responses.map(item => ({ ...item, phase: 'done', requester: item.recipient, recipient: item.responder })),
  ]
  return rows.map(item => {
    const recipientProfile = profiles?.get(item.recipient?.toLowerCase())
    return {
      id: `${item.phase}:${item.id}`,
      at: item.at,
      phase: item.phase,
      review: item.review,
      requester: item.requester,
      requester_profile: profiles?.get(item.requester?.toLowerCase()),
      recipient: item.recipient,
      recipient_profile: recipientProfile,
      next_owner: item.phase === 'done' ? null : item.recipient,
      next_owner_profile: recipientProfile,
      source_id: item.task_id,
      source_key: item.task_key,
      source_title: item.task_title,
      public_url: item.public_url,
    }
  }).sort((left, right) => (right.at || '').localeCompare(left.at || ''))
}

export function QueuedAsksContent({ project, grouped, page, snapshot, snapshotProfiles, viewer, projectNames }) {
  const [mode, setMode] = useState('compact')
  const items = snapshot ? snapshotRows(snapshot, snapshotProfiles) : page?.data?.items
  if (items?.length === 0 && (snapshot || (!page.pending && !page.error))) return null

  const headingId = grouped ? `request-log-${project}` : 'request-log-title'
  const Heading = grouped ? 'h3' : 'h2'
  return (
    <section className="queued-asks" aria-labelledby={headingId}>
      <div className="request-log-heading">
        <Heading id={headingId}>Request log</Heading>
        {items?.length > 0 && (
          <SegmentedControl label="Request log display" value={mode} options={displayModes} onChange={setMode} />
        )}
      </div>
      {page && <ResourceStatus resource={page} label="request log" />}
      {items?.length > 0 && (
        <ul className="report-outcomes request-log" aria-label="Requests" role="list" data-mode={mode}>
          {items.map(item => <RequestRow key={item.id} item={item} viewer={viewer}
            publicPreview={Boolean(snapshot)} verbose={mode === 'verbose'}
            projectLabel={projectNames?.get(item.source_key?.split('-')[0])} />)}
        </ul>
      )}
      {page && <LoadMore page={page} label="requests" />}
    </section>
  )
}

export default function QueuedAsks({ revision, project, grouped, viewer, projectNames }) {
  const page = useLoadedPages('/reports/request-log', projectNames ? { scope: 'all' } : { project }, revision, { limit: 10 })
  return <QueuedAsksContent project={project} grouped={grouped} page={page} viewer={viewer} projectNames={projectNames} />
}
