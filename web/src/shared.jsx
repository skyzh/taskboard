import React, { useId, useState } from 'react'
import {
  ArrowUpRight, Ban, Check, Circle, CircleQuestionMark, ClipboardList, Clock, CornerUpRight, Diamond,
  FileCheck, FileText, Flag, GitMerge, GitPullRequest, GitPullRequestClosed, Link2, ListChecks, MessageSquareText,
  MessagesSquare, Pause, PencilLine, Play, Plus, Rocket, RotateCcw, Tags, TriangleAlert, Undo2, Users, X,
} from 'lucide-react'
import { activityExtraText, waitLabel } from './workCopy.js'
import { Markdown, MarkdownInline } from './Markdown.jsx'
import { sourceLinkLabel } from './sourceLinks.js'
import { useTimeZone } from './timeZone.js'
import { ReportTime } from './ReportTime.jsx'
import { DemoControl, DemoUnavailable } from './DemoControl.jsx'

export const states = {
  human_review: 'Assigned to human',
  waiting: 'Waiting',
  in_progress: 'In progress',
  ready: 'Ready',
  parked: 'Parked',
  done: 'Done',
  closed: 'Closed',
}

export const actions = {
  add: 'Task created',
  update: 'Progress',
  log: 'Activity logged',
  wait: 'Wait recorded',
  resume: 'Resumed',
  'assign-human': 'Decision requested',
  'redirect-decision': 'Decision redirected',
  'withdraw-decision': 'Decision withdrawn',
  decide: 'Decision recorded',
  done: 'Completed',
  park: 'Parked',
  unpark: 'Returned to Ready',
  transfer: 'Transferred',
  'source-event': 'Source update',
}

const activityIcons = {
  add: Plus, start: Play, update: PencilLine, wait: Clock, resume: RotateCcw,
  done: Check, 'assign-human': CircleQuestionMark, decide: Diamond,
  'redirect-decision': CornerUpRight, 'withdraw-decision': RotateCcw,
  park: Pause, unpark: RotateCcw, transfer: CornerUpRight,
  'source-event': ArrowUpRight, log: MessageSquareText,
}

const activityAudits = {
  'edit-task-details': ['Ticket details edited', PencilLine],
  'checklist-add': ['Checklist step added', ListChecks],
  'checklist-start': ['Checklist step started', ListChecks],
  'checklist-complete': ['Checklist step completed', ListChecks],
  'checklist-rename': ['Checklist step renamed', ListChecks],
  'checklist-reopen': ['Checklist step reopened', ListChecks],
  'checklist-remove': ['Checklist step removed', ListChecks],
  'priority-change': ['Priority changed', Flag],
  'milestone-save': ['Milestone saved', Flag],
  'tag-add': ['Tag added', Tags],
  'tag-remove': ['Tag removed', Tags],
  'source-attach': ['Source attached', Link2],
  'reference-add': ['Reference added', Link2],
  'reference-relabel': ['Reference relabeled', Link2],
  'worker-delegate': ['Worker assigned', Users],
  'worker-start': ['Worker started', Play],
}

const workMarks = {
  'pull-opened': ['Pull request opened', GitPullRequest],
  'pull-review': ['GitHub review', FileCheck],
  'pull-merged': ['Pull request merged', GitMerge],
  'pull-closed': ['Pull request closed without merge', GitPullRequestClosed],
  'merge-review': ['Local merge reviewed', FileCheck],
  'local-merge': ['Merged locally', GitMerge],
  'release-released': ['Deployed', Rocket],
  'release-rolled_back': ['Deployment rolled back', Undo2],
  'release-rollback_failed': ['Deployment rollback failed', TriangleAlert],
  'release-rejected': ['Deployment rejected', Ban],
}

function pullActivityKind(event) {
  const { context } = event
  const githubLog = event.action === 'log' && context?.extension === 'github-events'
  if (githubLog && context.github_event === 'pull-merged') return 'pull-merged'
  if (githubLog && context.github_event === 'deployed') return 'release-released'
  if ((!githubLog && event.action !== 'source-event') || context?.source_kind !== 'github') return null
  if (context.event_kind?.startsWith('review-')) return 'pull-review'
  const source = event.links?.[0]?.url
  if (!/^https:\/\/github\.com\/[^/]+\/[^/]+\/pull\/[1-9]\d*\/?$/.test(source)) return null
  if (context.event_kind === 'opened') return 'pull-opened'
  if (context.event_kind === 'merged') return 'pull-merged'
  if (context.event_kind === 'closed' && context.pull_closed_without_merge === true) return 'pull-closed'
  return null
}

export function ActivityMark({ event }) {
  const context = event.context
  let kind = event.action
  let label = event.label || 'Activity'
  let Icon = Object.hasOwn(activityIcons, event.action) ? activityIcons[event.action] : FileText

  if (context?.closure) {
    kind = 'closed'
    label = 'Closed'
    Icon = X
  } else if (context?.worker_report_id) {
    kind = 'report'
    label = 'Worker report'
    Icon = ClipboardList
  } else if (context?.worker_disposition_id) {
    kind = 'report'
    label = 'Worker report disposition'
    Icon = ClipboardList
  } else {
    const workKind = context?.work_kind || pullActivityKind(event)
    if (Object.hasOwn(workMarks, workKind)) {
      [label, Icon] = workMarks[workKind]
      kind = workKind === 'pull-review' || workKind === 'merge-review' ? 'review' : workKind
    } else if (context?.stamp_verdict || context?.stamp_review) {
      kind = 'review'
      label = context.stamp_verdict ? 'Stamp review' : 'Stamp review requested'
      Icon = FileCheck
    } else if (context?.peer_correction) {
      label = 'Peer result corrected'
    } else if (context?.peer_ask_ticket && (event.action === 'log' || event.action === 'update')) {
      label = 'Peer request updated'
      Icon = MessagesSquare
    } else if (context?.ownership_action) {
      Icon = CornerUpRight
    } else if (Object.hasOwn(activityAudits, context?.audit?.action)) {
      [label, Icon] = activityAudits[context.audit.action]
    }
  }

  return (
    <span className={`activity-mark ${kind}`} role="img" aria-label={label} title={label}>
      <Icon size={14} aria-hidden="true" focusable="false" />
    </span>
  )
}

export function taskLabel(id, reference) {
  return reference || (typeof id === 'string' ? id : `T-${id}`)
}

export function taskHref(id, reference) {
  const anchor = reference && (id == null || !reference.startsWith('T-')) ? reference : id
  return `#task-${anchor}`
}

export function ownerHref(owner, section, project) {
  return `#${new URLSearchParams({ dri: owner, ...(section && { section }), ...(project && { project }) })}`
}

export function eventHref(id) {
  return `#event-${id}-log`
}

export function notificationLogFilters(fragment) {
  const match = /^notifications-log(?:\?project=([A-Z][A-Z0-9]{0,7})(?:&status=(pending|history))?(?:&kind=(ready|merge|wake))?(?:&agent=([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}))?)?$/.exec(fragment)
  if (!match) return null
  const [, project, state = 'all', kind = '', agent = ''] = match
  return { project, state, kind, agent }
}

export function signInHref(hash) {
  const login = './auth/login'
  const [anchor] = hash.split('?', 1)
  const taskLink = /^#task-(?:[A-Z][A-Z0-9]{0,7}-)?[1-9]\d{0,18}$/.test(anchor)
  const statsLink = /^#stats(?:\?(?:project=[A-Z][A-Z0-9]{0,7}(?:&period=(?:hour|day|week))?|period=(?:hour|day|week)(?:&project=[A-Z][A-Z0-9]{0,7})?))?$/.test(hash)
  const activityLink = /^#(?:activity|event-[1-9]\d{0,18}-log)(?:\?(?:actor=[A-Za-z0-9_%+.*-]{1,1920}&?)?(?:action=[a-z-]{1,30}&?)?(?:detail=all&?)?(?:hide_requests=true)?)?$/.test(hash)
  const goalActivityLink = /^#reports\?view=goal-activity&project=[A-Z][A-Z0-9]{0,7}(?:&agent=[A-Za-z0-9_%+.*-]{1,1920})?(?:&goal=[A-Z][A-Z0-9]{0,7}-[1-9]\d{0,18})?$/.test(hash)
  const returnToBoard = taskLink || statsLink || activityLink || goalActivityLink || /^#(?:notifications(?:-needs-me)?|reports(?:-all|\?project=[A-Z][A-Z0-9]{0,7})|settings|about|personal-codex)$/.test(hash)
    || (hash.startsWith('#') && notificationLogFilters(hash.slice(1)))
  return returnToBoard ? `${login}?fragment=${encodeURIComponent(taskLink ? anchor : hash)}` : login
}

export function Timestamp({ at, showDate = true, compact = false }) {
  const { formatTimestamp, formatCompactTimestamp } = useTimeZone()
  if (compact) return <time dateTime={at} title={at}>{formatCompactTimestamp(at)}</time>
  return <time dateTime={at} title={showDate ? undefined : formatTimestamp(at)}>{formatTimestamp(at, showDate)}</time>
}

export function RecordedActivity({ activity }) {
  if (!activity) return null
  return (
    <p className="last-activity">
      Last activity <a href={eventHref(activity.id)}><ReportTime at={activity.at} linked /></a>
    </p>
  )
}

const workerStates = {
  planned: 'Planned',
  running: 'Running',
  start_unconfirmed: 'Start unconfirmed',
  reported: 'Reported',
  collected: 'Collected',
  stopped: 'Stopped',
}

export function WorkerResult({ result }) {
  if (result !== 'blocked' && result !== 'completed') return null
  return <span className="worker-result">{result === 'blocked' ? 'Blocked' : 'Completed'}</span>
}

export function WorkerRunState({ run }) {
  const state = run.execution_state ?? (run.status === 'running' ? 'start_unconfirmed' : run.status)
  const role = { build: 'Build', review: 'Review' }[run.role] || run.role
  return <span>{role} · {workerStates[state] || 'Unknown'}{run.result && <> · <WorkerResult result={run.result} /></>}</span>
}

export function WorkerRunTiming({ run }) {
  const times = [
    ['Assigned', run.assigned_at],
    [run.execution_state === 'start_unconfirmed' ? 'Recorded start time' : 'Started', run.started_at],
    ['Reported', run.reported_at],
    [run.status === 'stopped' ? 'Stopped' : 'Collected', run.finalized_at],
  ].filter(([, at]) => at)
  if (!times.length) return null
  return (
    <p className="worker-times">
      {times.map(([label, at]) => <span key={label}>{label} <Timestamp at={at} /></span>)}
    </p>
  )
}

export function WorkerAssignment({ run }) {
  const effort = run.effort?.trim()
  const hasBrief = Boolean(run.brief?.trim())
  if (!effort && !hasBrief) return null
  return (
    <div className="worker-assignment">
      {effort && <p><strong>Assigned effort</strong> · {effort}</p>}
      {hasBrief && <Markdown text={run.brief} />}
    </div>
  )
}

export function Status({ state, compactOnPhone = false }) {
  const label = states[state] || state
  return (
    <span className={`status ${state}${compactOnPhone ? ' report-status' : ''}`}>
      {compactOnPhone ? <span>{label}</span> : label}
    </span>
  )
}

export function taskState(task) {
  return task.display_state || (task.context?.closure ? 'closed' : task.state)
}

export function TaskPriority({ priority, strip = false, short = false }) {
  const level = priority === 'high' || priority === 'low' ? priority : 'normal'
  const [name, segments] = { high: ['High', 3], normal: ['Normal', 2], low: ['Low', 1] }[level]
  const label = `Priority ${name}`
  if (strip) {
    return (
      <span className={`task-priority-strip ${level}`} role="img" aria-label={label} title={label}>
        {Array.from({ length: segments }, (_, index) => <span key={index} className="task-priority-segment" aria-hidden="true" />)}
      </span>
    )
  }
  if (short) return <span className="task-priority"><span className="report-priority-accessible">Priority </span>{name}</span>
  return <span className="task-priority">{label}</span>
}

export function DriWorkStatus({ inProgress, compact = false }) {
  if (!inProgress) return null
  if (compact) return <span className="dri-work-status dri-work-status-compact" role="img" aria-label="Tasks in progress" />
  return <span className="dri-task-state"><span className="dri-work-status" aria-hidden="true" />Tasks in progress</span>
}

export function BoardBrand({ href }) {
  return <DemoControl as="a" className="brand" href={href}><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>Taskboard</DemoControl>
}

export function TaskLink({ id, title, current, reference, searchQuery, publicHref }) {
  const anchor = taskHref(id, reference)
  const href = publicHref === undefined
    ? searchQuery ? `${anchor}?${new URLSearchParams({ 'from-search': searchQuery })}` : anchor
    : publicHref
  const label = <><span className="ticket-id">{taskLabel(id, reference)}</span> <MarkdownInline text={title} links={false} /></>
  return href ? <DemoControl as="a" href={href} aria-current={current ? 'true' : undefined}>{label}</DemoControl>
    : <DemoUnavailable className="static-task-title">{label}</DemoUnavailable>
}

export const peerRequestLabels = {
  asked: 'Asked',
  in_progress: 'In progress',
  acknowledgment: 'Ready for acknowledgment',
  done: 'Completed',
  canceled: 'Canceled',
  superseded: 'Superseded',
  closed_without_response: 'Closed without a response',
  closed: 'Closed',
}

export function PeerRequestContext({ task, showState = true }) {
  if (task.peer_request) {
    const { phase, source_id: sourceId, source_key: sourceKey } = task.peer_request
    const state = taskState(task)
    const label = phase === 'in_progress' ? states[state] || state : peerRequestLabels[phase]
    return (
      <p className="event-meta">
        {showState && <strong>{label}</strong>}
        <span>Request for <TaskLink id={sourceId} reference={sourceKey} /></span>
      </p>
    )
  }
  const linked = !task.context?.blocked_by && task.context?.peer_ask_ticket
  return linked ? <p className="event-meta">Peer request <TaskLink reference={linked} /></p> : null
}

export function ClosureReference({ context }) {
  const related = context?.closure?.related
  if (!related) return null
  return <p>Related task <TaskLink id={related} /></p>
}

export function AssigneeAvatar({ name, url, large = false }) {
  const [failed, setFailed] = useState(null)
  const size = large ? 36 : 20
  const className = `assignee-avatar${large ? ' large' : ''}`
  if (url && url !== failed) {
    return <img className={className} src={url} width={size} height={size} alt="" onError={() => setFailed(url)} />
  }
  const initials = name.trim().split(/\s+/).slice(0, 2).map(word => Array.from(word)[0]).join('').toUpperCase()
  return <span className={`${className} initials`} aria-hidden="true">{initials}</span>
}

export function profileName(key, profile) {
  const kind = /^raft:(agent|human):[a-f0-9-]{36}$/i.exec(key || '')?.[1]
  const name = [profile?.name, profile?.username].find(value => value && (!kind || value !== key))
  return name || (kind ? `Raft ${kind.toLowerCase()}` : key)
}

export function Assignee({ task, compact = false }) {
  if (!task.dri) return <span className="assignee">Unassigned</span>
  const profile = task.dri_profile
  const name = profileName(task.dri, profile)
  const project = task.key?.split('-')[0] || 'T'
  return (
    <span className="assignee">
      <AssigneeAvatar name={name} url={profile?.avatar_url} />
      <span>{!compact && 'Owner '}<a href={ownerHref(task.dri, undefined, project)}>{name}</a>{!compact && profile?.role && <> · {profile.role}</>}</span>
    </span>
  )
}

export function Dependency({ task, publicPreview = false, inlineWaiting = false }) {
  if (task.blockers) {
    const { tickets, cycle } = task.blockers
    return (
      <span>
        Blocked by {tickets.map((ticket, index) => (
          <React.Fragment key={index}>
            {index > 0 && ' → '}
            {publicPreview ? <DemoUnavailable as="strong">{taskLabel(ticket.id, ticket.key)}</DemoUnavailable>
              : <a href={taskHref(ticket.id, ticket.key)}>{taskLabel(ticket.id, ticket.key)}</a>}
            {' '}({states[ticket.state]})
          </React.Fragment>
        ))}
        {cycle != null && <strong> · Existing dependency cycle</strong>}
      </span>
    )
  }
  if (!task.waiting_on) return null
  const label = waitLabel(task)
  return <span>{inlineWaiting && label === 'Waiting on' ? 'on' : label} <strong>{profileName(task.waiting_on, task.waiting_profile)}</strong></span>
}

export function addFilterName(filters, key, profile) {
  if (!key) return
  const name = profileName(key, profile)
  const label = profile?.username && profile.username !== key && name !== profile.username
    ? `${name} (@${profile.username})`
    : name
  let available = label
  for (let index = 2; filters.has(available) && filters.get(available) !== key; index += 1) {
    available = `${label} ${index}`
  }
  filters.set(available, key)
}

function decodePath(pathname) {
  try {
    return decodeURIComponent(pathname)
  } catch {
    return pathname
  }
}

const githubSourceLabels = new Map([
  ['open', 'Open'], ['draft', 'Draft'], ['closed', 'Closed'], ['merged', 'Merged'],
])

function GitHubSourceStatus({ id, snapshot }) {
  if (!snapshot) return null
  const label = githubSourceLabels.get(snapshot.state)
  const text = !label ? 'Status unavailable' : snapshot.stale ? `Last seen ${label.toLowerCase()}` : label
  return <span id={id} className="github-source-status">{text}</span>
}

const ticketSourceKinds = new Map([
  ['github.com', ['github', 'GitHub source']],
  ['app.raft.build', ['raft', 'Raft source']],
  ['raft.build', ['raft', 'Raft source']],
  ['stamp.build', ['stamp', 'Stamp source']],
])

function TicketSourceMark({ url, local }) {
  const site = (url?.protocol === 'https:' || url?.protocol === 'http:') && ticketSourceKinds.get(url.hostname)
  if (site) return <span className="ticket-source-mark report-project-mark" data-kind={site[0]} role="img" aria-label={site[1]} />
  const Icon = local ? FileText : Link2
  return <span className="ticket-source-mark" role="img" aria-label={local ? 'Local file source' : 'Link source'}>
    <Icon size={16} aria-hidden="true" />
  </span>
}

export function EvidenceLinks({ links, label = 'Evidence', ticketSources = false }) {
  const listId = useId()
  if (!links?.length) return null
  return (
    <ul className={ticketSources ? 'evidence-links ticket-source-list' : 'evidence-links'} aria-label={label} role="list">
      {links.map((link, index) => {
        const { label, url } = link
        const statusId = link.github ? `${listId}-github-${index}` : undefined
        if (link.unavailable || !url) return (
          <li key={index}>{ticketSources && <TicketSourceMark local />}{label} <span className="evidence-unavailable">(local file)</span></li>
        )
        const parsed = URL.canParse(url) ? new URL(url) : null
        const protocol = parsed?.protocol
        const supported = protocol === 'http:' || protocol === 'https:' || protocol === 'file:'
        const preview = protocol === 'file:' && /^\.\/api\/v1\/local-evidence\/[1-9]\d*\/\d+$/.test(link.preview_url)
          ? link.preview_url
          : null
        return (
          <li key={index}>
            {ticketSources && <TicketSourceMark url={parsed} local={protocol === 'file:'} />}
            {supported ? <a href={url} aria-describedby={statusId}>{link.generated ? sourceLinkLabel(url, label) : label}</a> : <span>{label} {url}</span>}
            <GitHubSourceStatus id={statusId} snapshot={link.github} />
            {protocol === 'file:' && (
              <details className="path-disclosure">
                <summary aria-label={`Show local path for ${label}`}>Local path</summary>
                <code>{parsed.host ? `//${parsed.host}` : ''}{decodePath(parsed.pathname)}</code>
              </details>
            )}
            {preview && <LocalEvidencePreview key={preview} href={preview} label={label} />}
          </li>
        )
      })}
    </ul>
  )
}

function LocalEvidencePreview({ href, label }) {
  const [status, setStatus] = useState('closed')
  const expanded = status !== 'closed'
  const action = expanded ? 'Hide preview' : 'Preview image'

  return (
    <>
      <button className="evidence-preview-button" type="button" aria-expanded={expanded}
        aria-label={`${action} for ${label}`} onClick={() => setStatus(expanded ? 'closed' : 'loading')}>
        {action}
      </button>
      {expanded && (
        <div className="evidence-preview">
          {status !== 'unavailable' && <img src={href} alt={label} referrerPolicy="no-referrer"
            onLoad={() => setStatus('loaded')} onError={() => setStatus('unavailable')} />}
          <p role="status">
            {status === 'loading' && 'Loading image…'}
            {status === 'unavailable' && 'Preview unavailable. Open the file using its local path.'}
          </p>
        </div>
      )}
    </>
  )
}

export function Options({ options }) {
  return options?.length ? (
    <ul className="context-list" aria-label="Decision options">
      {options.map((option, index) => <li key={index}><MarkdownInline text={option} /></li>)}
    </ul>
  ) : null
}

export function ChecklistSteps({ steps, relatedTasks, editStep, showMissingTimes = false }) {
  return (
    <ol className="context-list checklist-steps" role="list">
      {steps.map(step => {
        const [Icon, status] = step.checked ? [Check, 'Complete'] : step.started_at ? [Play, 'In progress'] : [Circle, 'Pending']
        return (
          <li className="checklist-step" key={step.position}>
            <span className="checklist-step-mark" role="img" aria-label={status}>
              <Icon size={14} aria-hidden="true" focusable="false" />
            </span>
            <span className="checklist-step-number">{step.position}</span>
            <span className="checklist-step-copy">
              <MarkdownInline text={step.label} relatedTasks={relatedTasks} />
              {step.started_at && <small className="checklist-step-history">
                <span aria-hidden="true"> · </span>Started <Timestamp at={step.started_at} />
                {step.started_by && <> by {profileName(step.started_by, { name: step.started_by_name })}</>}
              </small>}
              {step.completed_at && <small className="checklist-step-history">
                <span aria-hidden="true"> · </span>Completed <Timestamp at={step.completed_at} />
                {step.completed_by && <> by {profileName(step.completed_by, { name: step.completed_by_name })}</>}
              </small>}
              {showMissingTimes && Boolean(step.checked) && !step.completed_at &&
                <small className="checklist-step-history">Completion time was not recorded.</small>}
            </span>
            {editStep?.(step)}
          </li>
        )
      })}
    </ol>
  )
}

export function ReminderWhen({ context, className }) {
  if (context?.defer_until && context.defer_at) {
    return <p className={className}>Deferred until <time dateTime={context.defer_until}>{context.defer_at.replace('T', ' ')}</time></p>
  }
  if (context?.review_after) {
    return <p className={className}>Review on or after <time dateTime={context.review_after}>{context.review_after}</time></p>
  }
  return null
}

export function OutcomeNote({ event, headline, withEvidence = true, choiceShown = false,
  waitShown = false, disclosure }) {
  const context = event.context || {}
  const text = headline == null ? event.details ?? event.note : activityExtraText(event, headline)
  const dependency = (event.waiting_on || event.blockers) && !waitShown
  const choice = !choiceShown && context.choice
  const evidence = withEvidence && event.links?.length > 0
  if (!text && !dependency && !choice && !evidence && !context.decision_reason &&
    !context.review_after && !context.defer_until && !context.choices?.length && !context.request_event_id) return null

  const body = (
    <div className="event-body">
      {text && <Markdown text={text} relatedTasks={event.related_tasks} />}
      {dependency && <p><Dependency task={event} /></p>}
      {choice && <p>Selected <strong><MarkdownInline text={choice} /></strong></p>}
      {context.decision_reason && <Markdown text={'Reason ' + context.decision_reason} />}
      <ReminderWhen context={context} />
      <Options options={context.choices} />
      {context.request_event_id && (
        <p><a href={eventHref(context.request_event_id)}>Decision request</a></p>
      )}
      {evidence && <EvidenceLinks links={event.links} />}
    </div>
  )
  return disclosure ? <details className={disclosure}><summary>Details</summary>{body}</details> : body
}

export function ResourceStatus({ resource, label }) {
  if (resource.pending && !resource.data) return <p className="section-message" role="status">Loading {label}…</p>
  if (!resource.error) return null
  return (
    <div className="section-message" role="status">
      <p>Unable to load {label}. {resource.error.message}</p>
      <button type="button" onClick={resource.retry}>Retry</button>
    </div>
  )
}

export function LoadMore({ page, label }) {
  if (page.data?.next_cursor == null) return null
  const action = page.moreError ? page.retry : page.loadMore
  return (
    <div className="list-continuation">
      <button type="button" aria-label={page.moreError ? `Retry loading more ${label}` : `More ${label}`}
        aria-disabled={!action} onClick={action || undefined}>{page.moreError ? 'Retry' : 'More'}</button>
      {page.loadingMore && <p role="status">Loading more {label}…</p>}
      {page.moreError && <p role="status">Unable to load more {label}. {page.moreError.message}</p>}
    </div>
  )
}
