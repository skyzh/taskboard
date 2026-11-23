import React, { useMemo, useState } from 'react'
import { useLoadedPages, useResource } from './data.js'
import {
  ActivityMark, AssigneeAvatar, DriWorkStatus, EvidenceLinks, LoadMore, ResourceStatus, TaskLink, WorkerResult,
  eventHref, ownerHref, profileName, taskHref, taskLabel,
} from './shared.jsx'
import './reports.css'

import { TaskRows } from './WorkRow.jsx'
import { groupOutcomes } from './Overview.jsx'
import { ReportTime } from './ReportTime.jsx'
import { GoalDeadline } from './DriGoal.jsx'
import { goalActivityFragment } from './GoalActivity.jsx'
import { Markdown, MarkdownInline } from './Markdown.jsx'
import { useTimeZone } from './timeZone.js'
import { briefSourceText } from './workCopy.js'
import QueuedAsks from './QueuedAsks.jsx'
import StampIntegration from './StampIntegration.jsx'
import { DemoControl, DemoUnavailable } from './DemoControl.jsx'

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
      : <a key={id} href={ownerHref(owner, id, project)}>{label}</a>
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
        {report.event_id ? <a href={eventHref(report.event_id)} aria-label={`Open worker report ${label}`}>{label}</a> : <span>{label}</span>}
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

export function PendingReports({ owner, name, initial, total, cursor, project, projectTitle, heading = true }) {
  const firstPage = useMemo(() => ({ items: initial, next_cursor: cursor }), [initial, cursor])
  const page = useLoadedPages('/reports/pending', { owner, project }, 0, { limit: 10, firstPage })
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
      {publicPreview ? <DemoControl as="summary">Goals</DemoControl> : (
        <div className="report-goals-heading">
          <Heading>Goals</Heading>
          {project && <a href={`#${goalActivityFragment(project, owner)}`}>Goal activity</a>}
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
                : <DemoControl as="a" href={publicPreview ? source : ticket ? taskHref(ticket) : ownerHref(owner, goalSections[kind], project)}>{label}</DemoControl>}
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
    <a href={eventHref(event.id)} aria-label={`${label}, ${formatTimestamp(event.at)}`}>
      <ReportTime at={event.at} linked />
    </a>
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

export function AssigneeReport({ entry, revision, project, projectTitle, publicPreview = false, tourTargets = {}, activeTarget }) {
  const { owner, profile, tasks, task_count: taskCount,
    pending_reports: reports, pending_reports_before: before, report_count: reportCount } = entry
  const name = profileName(owner, profile)
  const outcomes = groupOutcomes(entry.recent_outcomes || [])
  const progressCount = entry.task_counts?.in_progress
  const inProgress = progressCount == null ? tasks.some(task => task.state === 'in_progress') : progressCount > 0
  const OwnerHeading = projectTitle ? 'h3' : 'h2'
  const WorkHeading = projectTitle ? 'h4' : 'h3'
  const queueLink = `#${new URLSearchParams({ ...(project && { project }), section: 'queue-heading', owner })}`
  return (
    <li className="report-owner">
      <div className="report-owner-heading" id={tourTargets.owner}
        data-tour-current={tourTargets.owner === activeTarget && activeTarget ? true : undefined}>
        <AssigneeAvatar name={name} url={profile?.avatar_url} large />
        <div>
          <div className="dri-name-line">
            <OwnerHeading>
              <DemoControl as="a" href={publicPreview ? entry.public_url : ownerHref(owner, undefined, project)}>{name}</DemoControl>
              <DriWorkStatus inProgress={inProgress} compact />
            </OwnerHeading>
            {projectTitle && <span className="report-project-label">{projectTitle}</span>}
          </div>
          {entry.last_activity && (
            <p className="last-activity">
              Last activity {publicPreview ? <ReportTime at={entry.last_activity.at} />
                : <ReportActivityTime event={entry.last_activity} label="Last activity" />}
            </p>
          )}
        </div>
      </div>
      {tasks.length > 0 && (
        <div className="report-owner-section" id={tourTargets.current} data-tour-current={tourTargets.current === activeTarget && activeTarget ? true : undefined}>
          <WorkHeading>Current work</WorkHeading>
          <TaskRows items={tasks} owner={false} label={`Current work for ${name}`} report="summary" revision={revision}
            publicPreview={publicPreview} />
          {taskCount > tasks.length && <p className="report-note">Showing {tasks.length} of {taskCount} current tasks.</p>}
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
      {entry.goals?.length > 0 && <ReportGoals goals={entry.goals} owner={owner} project={project} publicPreview={publicPreview}
        target={tourTargets.goals} heading={WorkHeading} highlighted={tourTargets.goals === activeTarget && Boolean(activeTarget)} />}
      {outcomes.size > 0 && (
        <div className="report-owner-section" id={tourTargets.outcomes} data-tour-current={tourTargets.outcomes === activeTarget && activeTarget ? true : undefined}>
          <WorkHeading>Recent outcomes</WorkHeading>
          <ol className="report-outcomes" aria-label={`Recent outcomes for ${name}`} role="list">
            {[...outcomes].map(([id, events]) => <ReportOutcome key={id} event={events[0]} publicPreview={publicPreview} />)}
          </ol>
        </div>
      )}
      {(!publicPreview || entry.task_counts) && <div className="owner-links">
        <OwnerWorkLinks owner={owner} counts={entry.task_counts} project={project} summary publicPreview={publicPreview} />
        {outcomes.size > 0 && (publicPreview ? <DemoUnavailable>All outcomes</DemoUnavailable>
          : <a href={ownerHref(owner, 'outcomes-title', project)}>All outcomes</a>)}
        {publicPreview ? <DemoUnavailable>All tasks</DemoUnavailable> : <a href={queueLink}>All tasks</a>}
      </div>}
      {!publicPreview && reports.length > 0 && (
        <PendingReports owner={owner} project={project} projectTitle={projectTitle} name={name} initial={reports}
          total={reportCount} cursor={before} />
      )}
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
  tourOwner, tourTargets, activeTarget }) {
  if (!entries?.length) return null
  return (
    <ol className="report-list" aria-label={label} role="list">
      {entries.map(entry => (
        <AssigneeReport key={entry.owner} entry={entry} revision={revision} project={project} projectTitle={projectTitle}
          publicPreview={publicPreview} tourTargets={entry.owner === tourOwner ? tourTargets : undefined} activeTarget={activeTarget} />
      ))}
    </ol>
  )
}

export function ReportsHeading({ headingId, projectTitle, children }) {
  const Heading = projectTitle ? 'h2' : 'h1'
  return (
    <div className="page-heading">
      <Heading id={headingId}>{projectTitle || 'Reports'}</Heading>
      <div className="report-controls">{children}</div>
    </div>
  )
}

const projectLinkLabels = { raft: 'Raft', github: 'GitHub', stamp: 'Stamp' }

function ProjectLinks({ project, revision }) {
  const { data } = useResource(`/projects/${encodeURIComponent(project)}/links`, revision)
  if (!data?.links?.length) return null

  return (
    <nav className="report-project-links" aria-label="Project links">
      {data.links.map(({ kind, url, name }) => (
        <a key={kind} href={url} rel="noreferrer">
          <span className="report-project-mark" data-kind={kind} aria-hidden="true" />
          <span>{projectLinkLabels[kind]}{name && <> {name}</>}</span>
        </a>
      ))}
    </nav>
  )
}

const attentionLabels = { decision: 'Decision', request: 'Request', review: 'Review' }

function NeedsYou({ project, revision }) {
  const page = useLoadedPages('/notifications/needs-you', project ? { project } : { scope: 'all' }, revision, { limit: 2 })
  const items = page.data?.items
  const error = page.error && page.error.status !== 404
  if (!items?.length && !error) return null

  return (
    <section className="reports-needs-you" aria-labelledby="reports-needs-you-title">
      <h2 id="reports-needs-you-title">Needs you</h2>
      {error && <ResourceStatus resource={page} label="work that needs you" />}
      {items?.length > 0 && (
        <ul role="list">
          {items.map(item => {
            const requester = profileName(item.requester, item.requester_profile)
            return (
              <li key={item.id}>
                <a href={taskHref(item.ticket)}>
                  <span className="needs-you-task">
                    <span className="ticket-id">{item.ticket}</span> <MarkdownInline text={item.title} links={false} />
                  </span>
                  <span className="needs-you-action">{attentionLabels[item.kind]}{requester && <> from {requester}</>}</span>
                </a>
              </li>
            )
          })}
        </ul>
      )}
      <LoadMore page={page} label="items that need you" />
    </section>
  )
}

function ProjectReports({ revision, project, projectTitle, hideRequests, requestFilter, viewer, initialAssigneeView = 'active' }) {
  const [selection, setSelection] = useState(null)
  const selectedView = selection && selection.project === project ? selection.view : null
  const view = selectedView ?? initialAssigneeView
  const page = useLoadedPages('/reports', { view, project, ...(hideRequests && { hide_requests: true }) }, revision)
  const label = view === 'all' ? 'Assignees' : 'Assignees with open work'
  const empty = view === 'all' ? 'No assignees.' : 'No open work to report.'
  const headingId = projectTitle ? `reports-${project}` : 'reports'
  return (
    <section className={projectTitle ? 'project-reports' : 'reports'} aria-labelledby={headingId}>
      <ReportsHeading headingId={headingId} projectTitle={projectTitle}>
        {!projectTitle && <a href={project && project !== 'T'
          ? `#${new URLSearchParams({ project, section: 'team-overview' })}` : '#team-overview'}>Team overview</a>}
        {project && <a href={`#${goalActivityFragment(project)}`}>Goal activity</a>}
        {requestFilter}
        {(page.data || selectedView) && (
          <label>
            Assignees
            <select value={view} onChange={event => setSelection({ project, view: event.target.value })}>
              <option value="active">Active</option>
              <option value="all">All</option>
            </select>
          </label>
        )}
      </ReportsHeading>
      {!projectTitle && viewer && <NeedsYou key={viewer.id} project={project} revision={revision} />}
      {!projectTitle && <StampIntegration key={project} project={project} revision={revision} />}
      <ReportsLayout sidebar={<QueuedAsks revision={revision} project={project} grouped={Boolean(projectTitle)} viewer={viewer} />}>
        <ResourceStatus resource={page} label="reports" />
        {page.data?.items.length === 0 && <p className="section-message" role="status">{empty}</p>}
        <AssigneeReportList entries={page.data?.items} label={label} revision={revision} project={project} projectTitle={projectTitle} />
        <LoadMore page={page} label={projectTitle ? `assignees in ${projectTitle}` : 'assignees'} />
      </ReportsLayout>
      {!projectTitle && <ProjectLinks project={project} revision={revision} />}
    </section>
  )
}

export default function Reports({ project, projects, ...props }) {
  const [hideRequests, setHideRequests] = useState(false)
  const requestFilter = <label className="activity-mode">
    <input type="checkbox" checked={hideRequests} onChange={event => setHideRequests(event.target.checked)} />
    Hide peer requests
  </label>
  if (!projects) return <ProjectReports key={props.initialAssigneeView} {...props} project={project}
    hideRequests={hideRequests} requestFilter={requestFilter} />
  return (
    <section className="reports" aria-labelledby="reports">
      <ReportsHeading headingId="reports">{requestFilter}</ReportsHeading>
      {props.viewer && <NeedsYou key={props.viewer.id} revision={props.revision} />}
      {projects.map(({ prefix, title }) => (
        <ProjectReports key={prefix} {...props} project={prefix} projectTitle={title} hideRequests={hideRequests} />
      ))}
    </section>
  )
}
