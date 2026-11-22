import React, { useMemo, useState } from 'react'
import { useLoadedPages } from './data.js'
import {
  AssigneeAvatar, DriWorkStatus, EvidenceLinks, LoadMore, RecordedActivity, ResourceStatus, TaskLink, WorkerResult,
  eventHref, ownerHref, profileName,
} from './shared.jsx'
import './reports.css'

import { TaskRows } from './WorkRow.jsx'
import { OutcomeSummary, groupOutcomes } from './Overview.jsx'
import { ReportTime, ReportTimeProvider } from './ReportTime.jsx'
import { Markdown } from './Markdown.jsx'
import QueuedAsks from './QueuedAsks.jsx'

export const ownerWorkSections = [
  { state: 'human_review', id: 'human_review-title', title: 'Needs a decision', label: 'tasks needing a decision' },
  { state: 'waiting', id: 'waiting-title', title: 'Waiting', label: 'waiting tasks' },
  { state: 'in_progress', id: 'now-title', title: 'In progress', label: 'tasks in progress' },
  { state: 'ready', id: 'ready-title', title: 'Ready', label: 'ready tasks' },
]

export function OwnerWorkLinks({ owner, counts, project }) {
  return ownerWorkSections.map(({ state, id, title }) =>
    counts?.[state] !== 0 && (
      <a key={id} href={ownerHref(owner, id, project)}>
        {title}{counts?.[state] != null && <> <strong>{counts[state]}</strong></>}
      </a>
    ))
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

function ReportGoals({ goals, owner, project, heading: Heading }) {
  return (
    <div className="report-owner-section">
      <Heading>Goals</Heading>
      <ul className="report-goals" role="list">
        {goals.map(({ kind, summary, deadline }) => (
          <li key={kind + ':' + summary}>
            <a href={ownerHref(owner, goalSections[kind], project)}>{summary}</a>
            {kind === 'standing' && <span>Standing goal</span>}
            {deadline && <time dateTime={deadline}>{deadline}</time>}
          </li>
        ))}
      </ul>
    </div>
  )
}

export function AssigneeReport({ entry, revision, project, projectTitle, viewer }) {
  const { owner, profile, tasks, task_count: taskCount,
    pending_reports: reports, pending_reports_before: before, report_count: reportCount } = entry
  const name = profileName(owner, profile)
  const outcomes = groupOutcomes(entry.recent_outcomes || [])
  const OwnerHeading = projectTitle ? 'h3' : 'h2'
  const WorkHeading = projectTitle ? 'h4' : 'h3'
  const queueLink = `#${new URLSearchParams({ ...(project && { project }), section: 'queue-heading', owner })}`
  return (
    <li className="report-owner">
      <div className="report-owner-heading">
        <AssigneeAvatar name={name} url={profile?.avatar_url} large />
        <div>
          <div className="dri-name-line">
            <OwnerHeading>
              <a href={ownerHref(owner, undefined, project)}>{name}</a>
              <DriWorkStatus inProgress={entry.task_counts?.in_progress > 0 || tasks.some(task => task.state === 'in_progress')} />
            </OwnerHeading>
            {projectTitle && <span className="report-project-label">{projectTitle}</span>}
          </div>
          <RecordedActivity activity={entry.last_activity} />
        </div>
      </div>
      <div className="owner-links">
        <OwnerWorkLinks owner={owner} counts={entry.task_counts} project={project} />
        {outcomes.size > 0 && <a href={ownerHref(owner, 'outcomes-title', project)}>Recent outcomes</a>}
        <a href={queueLink}>All tasks</a>
      </div>
      {entry.goals?.length > 0 && (
        <ReportGoals goals={entry.goals} owner={owner} project={project} heading={WorkHeading} />
      )}
      {tasks.length > 0 && (
        <div className="report-owner-section">
          <WorkHeading>Current work</WorkHeading>
          <TaskRows items={tasks} owner={false} label={`Current work for ${name}`} report revision={revision}
            ownReport={viewer?.id === owner} />
          {taskCount > tasks.length && <p className="report-note">Showing {tasks.length} of {taskCount} current tasks.</p>}
        </div>
      )}
      {outcomes.size > 0 && (
        <div className="report-owner-section">
          <WorkHeading>Recent outcomes</WorkHeading>
          <ol className="outcome-list" aria-label={`Recent outcomes for ${name}`} role="list">
            {[...outcomes].map(([id, events]) => <OutcomeSummary key={id} events={events} />)}
          </ol>
        </div>
      )}
      {reports.length > 0 && (
        <PendingReports owner={owner} project={project} projectTitle={projectTitle} name={name} initial={reports}
          total={reportCount} cursor={before} />
      )}
    </li>
  )
}

function ProjectReports({ revision, project, projectTitle, viewer }) {
  const [selection, setSelection] = useState(null)
  const filter = selection && selection.project === project ? selection.view : undefined
  const page = useLoadedPages('/reports', { view: filter, project }, revision)
  const view = page.data?.roster ? 'all' : filter || 'active'
  const showFilters = view !== 'all' && Boolean(page.data || filter)
  const label = view === 'all' ? 'Assignees' : view === 'active' ? 'Assignees with open work' : 'Other assignees'
  const empty = view === 'all' ? 'No assignees.' : view === 'active' ? 'No open work to report.' : 'No other assignees.'
  const Heading = projectTitle ? 'h2' : 'h1'
  const headingId = projectTitle ? `reports-${project}` : 'reports'
  return (
    <section className={projectTitle ? 'project-reports' : 'reports'} aria-labelledby={headingId}>
      <div className="page-heading">
        <Heading id={headingId}>{projectTitle || 'Reports'}</Heading>
        <div className="report-controls">
          {!projectTitle && <a href="#team-overview">Team overview</a>}
          {showFilters && (
            <label>
              Assignees
              <select value={view} onChange={event => setSelection({ project, view: event.target.value })}>
                <option value="active">Open work</option>
                <option value="idle">Other assignees</option>
              </select>
            </label>
          )}
        </div>
      </div>
      <div className="reports-layout">
        <div className="reports-owners">
          <ResourceStatus resource={page} label="reports" />
          {page.data?.items.length === 0 && <p className="section-message" role="status">{empty}</p>}
          {page.data?.items.length > 0 && (
            <ReportTimeProvider>
              <ol className="report-list" aria-label={label} role="list">
                {page.data.items.map(entry => (
                  <AssigneeReport key={entry.owner} entry={entry} revision={revision}
                    project={project} projectTitle={projectTitle} viewer={viewer} />
                ))}
              </ol>
            </ReportTimeProvider>
          )}
          <LoadMore page={page} label={projectTitle ? `assignees in ${projectTitle}` : 'assignees'} />
        </div>
        <QueuedAsks revision={revision} viewer={viewer} project={project} grouped={Boolean(projectTitle)} />
      </div>
    </section>
  )
}

export default function Reports({ project, projects, ...props }) {
  if (!projects) return <ProjectReports {...props} project={project} />
  return (
    <section className="reports" aria-labelledby="reports">
      <div className="page-heading"><h1 id="reports">Reports</h1></div>
      {projects.map(({ prefix, title }) => (
        <ProjectReports key={prefix} {...props} project={prefix} projectTitle={title} />
      ))}
    </section>
  )
}
