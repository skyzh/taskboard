import React, { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { useLoadedPages, useResource } from './data.js'
import {
  ActivityMark, AssigneeAvatar, DriWorkStatus, EvidenceLinks, LoadMore, ResourceStatus, TaskLink, WorkerResult,
  eventHref, ownerHref, profileName, taskHref, taskLabel,
} from './shared.jsx'
import './reports.css'
import { projectHref } from './projectScope.js'

import { TaskRows } from './WorkRow.jsx'
import { groupOutcomes } from './Overview.jsx'
import { ReportTime } from './ReportTime.jsx'
import ReportPresence from './ReportPresence.jsx'
import { GoalDeadline } from './DriGoal.jsx'
import { goalActivityFragment } from './GoalActivity.jsx'
import { Markdown, MarkdownInline } from './Markdown.jsx'
import { useTimeZone } from './timeZone.js'
import { briefSourceText } from './workCopy.js'
import QueuedAsks from './QueuedAsks.jsx'
import StampIntegration from './StampIntegration.jsx'
import SegmentedControl from './SegmentedControl.jsx'
import { DemoUnavailable } from './DemoControl.jsx'

export const ownerWorkSections = [
  { state: 'human_review', id: 'human_review-title', title: 'Needs a decision', label: 'tasks needing a decision' },
  { state: 'waiting', id: 'waiting-title', title: 'Waiting', label: 'waiting tasks' },
  { state: 'in_progress', id: 'now-title', title: 'In progress', label: 'tasks in progress' },
  { state: 'ready', id: 'ready-title', title: 'Ready', label: 'ready tasks' },
]

export function OwnerWorkLinks({ owner, counts, project, summary = false, publicPreview = false }) {
  return ownerWorkSections.filter(({ state }) =>
    counts?.[state] !== 0 && (!summary || state === 'human_review' || state === 'ready')
  ).map(({ state, id, title }) => {
    const label = <>{title}{counts?.[state] != null && <> <strong>{counts[state]}</strong></>}</>
    return publicPreview ? <DemoUnavailable key={id}>{label}</DemoUnavailable>
      : <BoardLink key={id} href={ownerHref(owner, id, project)}>{label}</BoardLink>
  })
}

function PendingReport({ report }) {
  const label = `W-${report.run_id}`
  return (
    <li className="pending-report">
      <div className="outcome-title">
        <TaskLink reference={report.task_key} id={report.task_id} title={report.task_title} />
      </div>
      <p className="pending-report-meta">
        <span>{profileName(report.worker, report.worker_profile)}</span>
        {report.event_id ? <BoardLink href={eventHref(report.event_id)} aria-label={`Open worker report ${label}`}>{label}</BoardLink> : <span>{label}</span>}
        <WorkerResult result={report.result} />
        {report.reported_at && <span>Reported <ReportTime at={report.reported_at} /></span>}
      </p>
      {(report.report || report.links?.length > 0) && (
        <details className="report-details">
          <summary aria-label={`Worker report and evidence for ${label}`}>Worker report and evidence</summary>
          {report.report && <Markdown text={report.report} />}
          <EvidenceLinks links={report.links} />
        </details>
      )}
    </li>
  )
}

export function PendingReports({ owner, name, initial, total, cursor, project, projectTitle, heading = true, reportFilters }) {
  const firstPage = useMemo(() => ({ items: initial, next_cursor: cursor }), [initial, cursor])
  const page = useLoadedPages('/reports/pending', { owner, ...(reportFilters || { project }) }, 0, { limit: 10, firstPage })
  const reports = new Map()
  for (const report of page.data?.items || initial) {
    if (!reports.has(report.run_id)) reports.set(report.run_id, report)
  }
  const visible = [...reports.values()]
  const showCount = cursor != null || visible.length < total
  const recipient = projectTitle ? `${name} in ${projectTitle}` : name
  const Heading = projectTitle ? 'h4' : 'h3'

  return (
    <div className="pending-reports">
      {heading && <Heading>Worker reports to review</Heading>}
      <ol className="report-list" aria-label={`Worker reports ${recipient} needs to review`} role="list">
        {visible.map(report => <PendingReport key={report.run_id} report={report} />)}
      </ol>
      {showCount && <p className="report-note" role="status">Showing {visible.length} of {total} worker reports.</p>}
      <ResourceStatus resource={page} label={`reports for ${recipient}`} />
      <LoadMore page={page} label={`reports for ${recipient}`} />
    </div>
  )
}

const goalSections = {
  standing: 'dri-standing-goal-title',
  original: 'dri-goal-title',
  milestone: 'dri-milestones-title',
}

function ReportGoals({ goals, owner, project, publicPreview, target, highlighted, heading: Heading }) {
  const Panel = publicPreview ? 'details' : 'div'
  return (
    <Panel className="report-owner-section report-goals-panel"
      id={target} data-tour-current={highlighted || undefined}>
      {publicPreview ? <summary>Goals</summary> : (
        <div className="report-goals-heading">
          <Heading>Goals</Heading>
          {project && <BoardLink href={`#${goalActivityFragment(project, owner)}`}>Goal activity</BoardLink>}
        </div>
      )}
      <ul className="report-goals" role="list">
        {goals.map(({ kind, summary, deadline, ticket, public_url: source }) => {
          const label = ticket == null ? summary : (
            <><span className="ticket-id">{taskLabel(ticket)}</span>{' '}<span>{summary}</span></>
          )
          return (
            <li key={ticket || kind + ':' + summary}>
              {publicPreview && !source ? <DemoUnavailable className="report-goal-text">{label}</DemoUnavailable>
                : <BoardLink href={publicPreview ? source : ticket ? taskHref(ticket) : ownerHref(owner, goalSections[kind], project)}>{label}</BoardLink>}
              {kind === 'standing' && <span>Standing goal</span>}
              {deadline && <GoalDeadline deadline={deadline} />}
            </li>
          )
        })}
      </ul>
    </Panel>
  )
}

function ReportActivityTime({ event, label }) {
  const { formatTimestamp } = useTimeZone()
  return (
    <BoardLink href={eventHref(event.id)} aria-label={`${label}, ${formatTimestamp(event.at)}`}>
      <ReportTime at={event.at} linked />
    </BoardLink>
  )
}

function ReportOutcome({ event, publicPreview }) {
  const choice = briefSourceText(event.context?.choice)
  return (
    <li className="report-outcome" id={`event-${event.id}-outcome`} tabIndex={-1}>
      <ActivityMark event={event} />
      <div className="report-outcome-title">
        <TaskLink reference={event.task_key} id={event.task_id} title={event.task_title}
          publicHref={publicPreview ? event.public_url || null : undefined} />
        {choice && <> · <MarkdownInline text={choice} links={!publicPreview} /></>}
      </div>
      {publicPreview
        ? <ReportTime at={event.at} linked />
        : <ReportActivityTime event={event} label={`Open outcome for ${taskLabel(event.task_id, event.task_key)}`} />}
    </li>
  )
}

function WatchedTickets({ owner, name, project, initial, revision, projectNames, hideRequests }) {
  const filters = { owner, ...(project ? { project } : { scope: 'all' }), ...(hideRequests && { hide_requests: true }) }
  const page = useLoadedPages('/reports/watched-tickets', filters, revision, { limit: 10, firstPage: initial })
  const tasks = page.data?.items || initial.items
  return (
    <div className="report-owner-section">
      <h3>Watched tickets</h3>
      <TaskRows items={tasks} owner={false} label={`Watched tickets for ${name}`} report="summary"
        revision={revision} projectNames={projectNames} />
      <ResourceStatus resource={page} label={`watched tickets for ${name}`} />
      <LoadMore page={page} label={`watched tickets for ${name}`} />
    </div>
  )
}

function ownerQueueHref(owner, project, filters = {}) {
  return `#${new URLSearchParams({
    ...(project ? { project } : { scope: 'all' }),
    section: 'queue-heading', owner, ...filters,
  })}`
}

function compareSummaryGoals(left, right) {
  if (left.kind === 'standing') return right.kind === 'standing' ? 0 : -1
  if (right.kind === 'standing') return 1
  if (!left.deadline) return right.deadline ? 1 : 0
  if (!right.deadline) return -1
  return left.deadline.localeCompare(right.deadline)
}

export function AssigneeReport({ entry, revision, project, projectTitle, publicPreview = false, tourTargets = {}, activeTarget,
  projectNames, reportFilters }) {
  const { owner, profile, tasks = [], task_count: taskCount, watching_person: watchingPerson,
    project_unfinished_counts: unfinishedCounts, watched_tickets: watchedTickets, pending_reports: reports = [],
    pending_reports_before: before, report_count: reportCount } = entry
  const name = owner ? profileName(owner, profile) : 'Unassigned'
  const outcomes = groupOutcomes(entry.recent_outcomes || [])
  const progressCount = entry.task_counts?.in_progress
  const inProgress = progressCount == null ? tasks.some(task => task.state === 'in_progress') : progressCount > 0
  const waitingCount = (entry.task_counts?.waiting ?? 0) + (entry.task_counts?.human_review ?? 0)
  const workCounts = [
    progressCount > 0 && `${progressCount} in progress`,
    waitingCount > 0 && `${waitingCount} waiting`,
  ].filter(Boolean).join(' · ')
  const previewTasks = tasks.slice(0, 3)
  const moreTasks = (taskCount ?? tasks.length) - previewTasks.length
  const goals = entry.goals || []
  const previewGoals = [...goals].sort(compareSummaryGoals).slice(0, 3)
  const moreGoals = goals.length - previewGoals.length
  const OwnerHeading = projectTitle ? 'h3' : 'h2'
  const WorkHeading = projectTitle ? 'h4' : 'h3'
  const projectChoices = entry.projects || []
  const multipleProjects = projectChoices.length > 1
  const showProjectLinks = Boolean(projectNames && projectChoices.length)
  const ownerProject = project || projectChoices[0]
  const scope = projectNames ? 'all' : project
  const activityLink = projectHref(`#activity?${new URLSearchParams({ actor: owner, detail: 'all' })}`, scope)
  const queueFilters = {
    ...(reportFilters?.hide_requests && { hide_requests: 'true' }),
    ...(reportFilters?.view === 'watching' && { watching: 'true' }),
  }
  const ownerLinks = owner && (showProjectLinks || watchingPerson !== false) && (!publicPreview || entry.task_counts) && (
    <div className="owner-links">
      {moreTasks > 0 && !publicPreview && (
        <BoardLink href={ownerQueueHref(owner, project, { current_work: 'all', ...queueFilters })}
          aria-label={`+${moreTasks} more ${moreTasks === 1 ? 'task' : 'tasks'} for ${name}`}>
          +{moreTasks} more {moreTasks === 1 ? 'task' : 'tasks'}
        </BoardLink>
      )}
      {moreGoals > 0 && !publicPreview && (
        <BoardLink href={`#${goalActivityFragment(project ?? null, owner, null, reportFilters?.view || 'active')}`}
          aria-label={`+${moreGoals} more ${moreGoals === 1 ? 'goal' : 'goals'} for ${name}`}>
          +{moreGoals} more {moreGoals === 1 ? 'goal' : 'goals'}
        </BoardLink>
      )}
      {showProjectLinks ? projectChoices.map(prefix => {
        const count = unfinishedCounts?.[prefix]
        const href = count == null ? ownerHref(owner, undefined, prefix)
          : ownerQueueHref(owner, prefix, { unfinished: 'true', ...queueFilters })
        return <BoardLink key={prefix} href={href}>
          {projectNames.get(prefix) || prefix}{count != null && ` (${count})`}
        </BoardLink>
      }) : <>
        <OwnerWorkLinks owner={owner} counts={entry.task_counts} project={ownerProject} summary publicPreview={publicPreview} />
        {outcomes.size > 0 && (publicPreview ? <DemoUnavailable>All outcomes</DemoUnavailable>
          : <BoardLink href={ownerHref(owner, 'outcomes-title', ownerProject)}>All outcomes</BoardLink>)}
        {publicPreview ? <DemoUnavailable>All tasks</DemoUnavailable> : <BoardLink href={ownerQueueHref(owner, ownerProject)}>All tasks</BoardLink>}
      </>}
      {!publicPreview && watchingPerson !== false && <BoardLink href={activityLink} aria-label={`Activity by ${name}`}>Activity</BoardLink>}
    </div>
  )
  return (
    <li className="report-owner">
      <div className="report-owner-heading" id={tourTargets.owner}
        data-tour-current={tourTargets.owner === activeTarget && activeTarget ? true : undefined}>
        {owner && <AssigneeAvatar name={name} url={profile?.avatar_url} large />}
        <div>
          <div className="dri-name-line">
            <OwnerHeading>
              {!owner || multipleProjects ? name : (
                <BoardLink href={publicPreview ? entry.public_url : ownerHref(owner, undefined, ownerProject)}>{name}</BoardLink>
              )}
              {profile?.short_role && <span className="report-short-role">{' · '}{profile.short_role}</span>}
              {watchingPerson !== false && <DriWorkStatus inProgress={inProgress} compact />}
            </OwnerHeading>
            {projectTitle && <span className="report-project-label">{projectTitle}</span>}
          </div>
          {(workCounts || entry.last_activity) && (
            <p className="last-activity">
              {workCounts}
              {entry.last_activity && <>
                {workCounts && ' · '}Last activity {publicPreview ? <ReportTime at={entry.last_activity.at} />
                  : <ReportActivityTime event={entry.last_activity} label="Last activity" />}
              </>}
            </p>
          )}
        </div>
      </div>
      {watchedTickets?.items.length > 0 && (
        <WatchedTickets owner={owner} name={name} project={project} initial={watchedTickets}
          revision={revision} projectNames={projectNames} hideRequests={reportFilters?.hide_requests} />
      )}
      {previewTasks.length > 0 && (
        <div className="report-owner-section report-owner-current" id={tourTargets.current}
          data-tour-current={tourTargets.current === activeTarget && activeTarget ? true : undefined}>
          <TaskRows items={previewTasks} owner={false} label={`Current work for ${name}`} report="summary" revision={revision}
            publicPreview={publicPreview} projectNames={projectNames} />
        </div>
      )}
      {!publicPreview && entry.legacy_peer_waits?.length > 0 && (
        <div className="report-owner-section">
          <WorkHeading>Waiting on {name}</WorkHeading>
          <ul className="report-outcomes" aria-label={`Requests for ${name}`} role="list">
            {entry.legacy_peer_waits.map(wait => (
              <li className="ask-row" key={wait.id}>
                <span>Request from {wait.requester}</span>
                <ReportTime at={wait.at} />
                <TaskLink reference={wait.task_key} id={wait.task_id} title={wait.task_title} />
              </li>
            ))}
          </ul>
        </div>
      )}
      {(previewGoals.length > 0 || outcomes.size > 0) && ownerLinks}
      {previewGoals.length > 0 && <ReportGoals goals={previewGoals} owner={owner}
        project={multipleProjects ? undefined : ownerProject} publicPreview={publicPreview}
        target={tourTargets.goals} heading={WorkHeading} highlighted={tourTargets.goals === activeTarget && Boolean(activeTarget)} />}
      {outcomes.size > 0 && (
        <div className="report-owner-section" id={tourTargets.outcomes} data-tour-current={tourTargets.outcomes === activeTarget && activeTarget ? true : undefined}>
          <WorkHeading>Recent outcomes</WorkHeading>
          <ol className="report-outcomes" aria-label={`Recent outcomes for ${name}`} role="list">
            {[...outcomes].map(([id, events]) => <ReportOutcome key={id} event={events[0]} publicPreview={publicPreview} />)}
          </ol>
        </div>
      )}
      {!publicPreview && reports.length > 0 && (
        <PendingReports owner={owner} project={project} projectTitle={projectTitle} name={name} initial={reports}
          total={reportCount} cursor={before} reportFilters={reportFilters} />
      )}
      {previewGoals.length === 0 && outcomes.size === 0 && ownerLinks}
    </li>
  )
}

export function ReportsLayout({ children, sidebar }) {
  return (
    <div className={`reports-layout${sidebar ? ' with-sidebar' : ''}`}>
      <div className="reports-owners">{children}</div>
      {sidebar}
    </div>
  )
}

export function AssigneeReportList({ entries, label, revision, project, projectTitle, publicPreview,
  tourOwner, tourTargets, activeTarget, projectNames, reportFilters }) {
  if (!entries?.length) return null
  return (
    <ol className="report-list" aria-label={label} role="list">
      {entries.map(entry => (
        <AssigneeReport key={entry.owner} entry={entry} revision={revision} project={project} projectTitle={projectTitle}
          projectNames={projectNames} reportFilters={reportFilters}
          publicPreview={publicPreview} tourTargets={entry.owner === tourOwner ? tourTargets : undefined} activeTarget={activeTarget} />
      ))}
    </ol>
  )
}

export function ReportsHeading({ headingId, projectTitle, children, focusable, projectPicker, headingAside }) {
  const Heading = projectTitle ? 'h2' : 'h1'
  const title = <Heading id={headingId} tabIndex={focusable ? -1 : undefined}>{projectTitle || 'Reports'}</Heading>
  return (
    <div className="reports-heading">
      {projectPicker}
      <div className="page-heading">
        {headingAside ? <div className="reports-title">{title}{headingAside}</div> : title}
        <div className="report-controls">{children}</div>
      </div>
    </div>
  )
}

const projectLinkLabels = { raft: 'Raft', github: 'GitHub', stamp: 'Stamp' }

function ProjectLinks({ project, revision }) {
  const path = project ? `/projects/${encodeURIComponent(project)}/links` : null
  const { data } = useResource(path, revision)
  const links = data?.links?.filter(link => Object.hasOwn(projectLinkLabels, link.kind))
  if (!links?.length) return null

  return (
    <nav className="report-project-links" aria-label="Project links">
      {links.map(({ kind, url, name }) => (
        <BoardLink key={kind} href={url} rel="noreferrer">
          <span className="report-project-mark" data-kind={kind} aria-hidden="true" />
          <span>{projectLinkLabels[kind]}{name && <> {name}</>}</span>
        </BoardLink>
      ))}
    </nav>
  )
}

const attentionLabels = { decision: 'Decision', request: 'Request', review: 'Review' }

export function NeedsYou({ project, revision, showAllAsks = false }) {
  const page = useLoadedPages('/notifications/needs-you', project ? { project } : { scope: 'all' }, revision, { limit: 10 })
  const items = page.data?.items
  const error = page.error && page.error.status !== 404
  const list = useRef(null)
  const keyboardContinuation = useRef(null)
  useLayoutEffect(() => {
    const continuation = keyboardContinuation.current
    if (!continuation) return
    if (continuation.project !== project) {
      keyboardContinuation.current = null
      return
    }
    const nextLink = list.current?.children[continuation.index]?.firstElementChild
    if (!nextLink) return
    keyboardContinuation.current = null
    const { button } = continuation
    if (document.activeElement === button || (!button.isConnected && document.activeElement === document.body)) {
      nextLink.focus()
    }
  }, [items, project])
  if (!items?.length && !error) return null

  return (
    <section className="reports-needs-you" aria-labelledby="reports-needs-you-title">
      <div className="needs-you-heading">
        <h2 id="reports-needs-you-title">Needs you</h2>
        {showAllAsks && <BoardLink href="#notifications-needs-me">All asks</BoardLink>}
      </div>
      {error && <ResourceStatus resource={page} label="work that needs you" />}
      {items?.length > 0 && (
        <ul ref={list} role="list">
          {items.map(item => {
            const requester = profileName(item.requester, item.requester_profile)
            return (
              <li key={item.id}>
                <BoardLink href={taskHref(item.ticket)}>
                  <span className="needs-you-task">
                    <span className="ticket-id">{item.ticket}</span> <MarkdownInline text={item.title} links={false} />
                  </span>
                  <span className="needs-you-action">
                    <span>{attentionLabels[item.kind]}{requester && <> from {requester}</>}</span>
                    {item.asked_at && <span className="needs-you-age">Asked <ReportTime at={item.asked_at} linked /></span>}
                  </span>
                </BoardLink>
              </li>
            )
          })}
        </ul>
      )}
      <LoadMore page={page} label="items that need you" onActivate={event => {
        if (event.detail === 0) {
          keyboardContinuation.current = { button: event.currentTarget, index: items.length, project }
        }
      }} />
    </section>
  )
}

function ProjectReports({ revision, project, projectNames, hideRequests, viewer, view }) {
  const watching = view === 'watching'
  const filters = {
    view, ...(projectNames ? { scope: 'all' } : { project }), ...(hideRequests && { hide_requests: true }),
    ...(!watching && viewer?.kind === 'human' && { include_self: true }),
  }
  const page = useLoadedPages('/reports', { ...filters, summary: true }, revision)
  const label = watching ? 'Watching' : view === 'all' ? 'Assignees' : 'Assignees with open work'
  const empty = watching ? 'Watch a person, DRI, or ticket to see their work here.' : view === 'all' ? 'No assignees.'
    : 'No open work to report.'
  return (
    <ReportsLayout sidebar={watching ? null :
      <QueuedAsks revision={revision} project={project} projectNames={projectNames} viewer={viewer} />}>
      <ResourceStatus resource={page} label="reports" />
      {page.data?.items.length === 0 && <p className="section-message" role="status">{empty}</p>}
      <AssigneeReportList entries={page.data?.items} label={label} revision={revision} project={project}
        projectNames={projectNames} reportFilters={filters} />
      <LoadMore page={page} label="assignees" />
    </ReportsLayout>
  )
}

const assigneeViews = [
  { value: 'active', label: 'Active' },
  { value: 'all', label: 'All' },
]

const signedAssigneeViews = [...assigneeViews, { value: 'watching', label: 'Watching' }]

export default function Reports({
  revision, viewer, project, projects, assigneeView = 'active',
  onAssigneeViewChange, headerAction, projectPicker, presenceScope,
}) {
  const [hideRequests, setHideRequests] = useState(false)
  const view = assigneeView === 'watching' && !viewer ? 'active' : assigneeView
  const reportProps = { revision, viewer, view, hideRequests }
  const projectNames = useMemo(() => projects && new Map(projects.map(({ prefix, title }) => [prefix, title])), [projects])
  const teamOverview = projectHref('#team-overview', projects ? 'all' : project)
  return (
    <section className="reports" aria-labelledby="reports">
      <ReportsHeading headingId="reports" projectPicker={projectPicker}
        headingAside={viewer?.kind === 'human' && presenceScope &&
          <ReportPresence key={`${viewer.id}:${presenceScope}`} scope={presenceScope} />}>
        <BoardLink href={teamOverview}>Team overview</BoardLink>
        {!projects && project && <BoardLink href={`#${goalActivityFragment(project)}`}>Goal activity</BoardLink>}
        {headerAction}
        <label className="activity-mode">
          <input type="checkbox" checked={hideRequests} onChange={event => setHideRequests(event.target.checked)} />
          Hide peer requests
        </label>
        <SegmentedControl className="report-assignee-views" label="Assignees" value={view}
          options={viewer ? signedAssigneeViews : assigneeViews} onChange={onAssigneeViewChange} />
      </ReportsHeading>
      {viewer && view !== 'watching' && <NeedsYou key={viewer.id} project={projects ? undefined : project} revision={revision} showAllAsks />}
      <ProjectReports key={projects ? 'all' : project} {...reportProps}
        project={projects ? undefined : project} projectNames={projectNames} />
      {!projects && <>
        <ProjectLinks project={project} revision={revision} />
        <StampIntegration key={project} project={project} revision={revision} />
      </>}
    </section>
  )
}
