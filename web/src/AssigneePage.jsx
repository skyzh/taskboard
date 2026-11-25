import React from 'react'
import { queryPath, useLoadedPages, useResource } from './data.js'
import { ActivityDate, Outcomes } from './Overview.jsx'
import { NeedsYou, OwnerWorkLinks, PendingReports, ownerWorkSections } from './Reports.jsx'
import {
  ActivityMark, AssigneeAvatar, DriWorkStatus, LoadMore, ResourceStatus, Timestamp, eventHref, ownerHref, profileName,
  taskLabel,
} from './shared.jsx'
import { TaskRows } from './WorkRow.jsx'
import DriWorkers from './DriWorkers.jsx'
import DriGoal, { DriMilestones } from './DriGoal.jsx'
import { Markdown, MarkdownInline } from './Markdown.jsx'
import { useTimeZone } from './timeZone.js'
import { activityHeadline, briefSourceText } from './workCopy.js'

function OwnerWork({ owner, state, id, title, label, revision, project }) {
  const page = useLoadedPages('/tasks', { dri: owner, state, project, order: state === 'ready' ? 'priority' : undefined }, revision)
  return (
    <section className="feed-section" aria-labelledby={id}>
      <h2 id={id}>{title}</h2>
      <ResourceStatus resource={page} label={label} />
      <TaskRows items={page.data?.items} owner={false} label={title} report revision={revision} />
      {page.data?.items.length === 0 && <p className="section-message">No {label}.</p>}
      <LoadMore page={page} label={label} />
    </section>
  )
}

function AssigneeActivity({ owner, project, revision }) {
  const { formatDay } = useTimeZone()
  const page = useLoadedPages('/activity', { actor: owner, project, detail: 'reports' }, revision, { limit: 10 })
  const days = new Map()
  for (const event of page.data?.items || []) {
    const day = formatDay(event.at)
    if (!days.has(day)) days.set(day, [])
    days.get(day).push(event)
  }
  return (
    <section className="feed-section" aria-labelledby="assignee-activity-title">
      <h2 id="assignee-activity-title" tabIndex={-1}>What was done</h2>
      <ResourceStatus resource={page} label="reported activity" />
      {page.data?.items.length === 0 && <p className="section-message">No reported activity in this project.</p>}
      {[...days].sort(([left], [right]) => right.localeCompare(left)).map(([day, events]) => (
        <div className="outcome-day" key={day}>
          <h3><ActivityDate day={day} /></h3>
          <ul className="assignee-activity-list" role="list">
            {events.map(event => {
              const recorded = activityHeadline(event)
              const headline = recorded && recorded !== event.label ? recorded : event.task_title || event.label
              return (
                <li key={event.id}>
                  <a href={eventHref(event.id)}>
                    <ActivityMark event={event} />
                    <span>
                      <span className="assignee-activity-headline"><MarkdownInline text={headline} links={false} /></span>
                      {' '}<span className="assignee-activity-meta">
                        {taskLabel(event.task_id, event.task_key)} · <Timestamp at={event.at} showDate={false} />
                      </span>
                    </span>
                  </a>
                </li>
              )
            })}
          </ul>
        </div>
      ))}
      <LoadMore page={page} label="reported activity" />
    </section>
  )
}

export default function AssigneePage({ owner, section, revision, project, reportsHref = '#reports', canEditGoal, viewer }) {
  const resource = useResource(queryPath('/assignee', { owner, project }), revision)
  const entry = resource.data
  if (!entry) return <ResourceStatus resource={resource} label="assignee profile" />
  const profile = entry.profile
  const agentReport = profile?.kind === 'agent'
  const showOutcomes = agentReport || entry.has_outcomes !== false || section === 'outcomes-title'
  const hasOutcomeLink = agentReport || entry.has_outcomes
  const name = profileName(owner, profile)
  const role = briefSourceText(profile?.role)
  const about = [!role && profile?.role, profile?.description].filter(Boolean).join('\n\n')
  const reports = entry.pending_reports
  const milestones = entry.milestones || []
  const showMilestones = milestones.length > 0 || canEditGoal
  const showStandingGoal = Boolean(entry.standing_goal || canEditGoal)
  const counts = entry.task_counts
  const hasWork = !counts || ownerWorkSections.some(({ state }) => counts[state] > 0)
  const sections = ownerWorkSections.filter(({ state, id }) => counts?.[state] !== 0 || section === id)
  const ready = sections.find(({ state }) => state === 'ready')
  return (
    <>
      <a className="back-link" href={reportsHref}>Back to Reports</a>
      <header id="assignee-title" className="assignee-page-heading">
        <AssigneeAvatar name={name} url={profile?.avatar_url} large />
        <div>
          <div className="dri-name-line">
            <h1>{name}<DriWorkStatus inProgress={entry.in_progress} /></h1>
            <DriWorkers owner={owner} project={project} counts={entry.worker_counts} revision={revision} />
          </div>
          {role && <p>{role}</p>}
          {about && (
            <details className="report-details">
              <summary>About {name}</summary>
              <Markdown text={about} className="assignee-description" />
            </details>
          )}
        </div>
      </header>
      {viewer?.kind === 'human' && viewer.id === owner && <NeedsYou project={project} revision={revision} />}
      {(showStandingGoal || showMilestones || entry.goal || hasWork || hasOutcomeLink || reports.length > 0) && (
        <nav className="owner-links profile-index" aria-label={`${name}’s work sections`}>
          {showStandingGoal && <a href={ownerHref(owner, 'dri-standing-goal-title', project)}>Standing goal</a>}
          {entry.goal && <a href={ownerHref(owner, 'dri-goal-title', project)}>Original dated goal</a>}
          {showMilestones && <a href={ownerHref(owner, 'dri-milestones-title', project)}>Dated milestones</a>}
          <OwnerWorkLinks owner={owner} project={project} counts={counts} />
          {hasOutcomeLink && <a href={ownerHref(owner, 'outcomes-title', project)}>Recent outcomes</a>}
          {agentReport && <a href={ownerHref(owner, 'assignee-activity-title', project)}>What was done</a>}
          {reports.length > 0 && (
            <a href={ownerHref(owner, 'reports-pending-title', project)}>Worker reports <strong>{entry.report_count}</strong></a>
          )}
        </nav>
      )}
      {showStandingGoal && <DriGoal goal={entry.standing_goal} project={project} standing editable={canEditGoal} onChanged={resource.retry} />}
      {entry.goal && <DriGoal goal={entry.goal} project={project} editable={canEditGoal} onChanged={resource.retry} />}
      {showMilestones && <DriMilestones milestones={milestones} project={project} editable={canEditGoal} onChanged={resource.retry} />}
      {sections.filter(item => item.state !== 'ready').map(item =>
        <OwnerWork key={item.state} owner={owner} project={project} revision={revision} {...item} />)}
      {showOutcomes && <Outcomes revision={revision} owner={owner} project={project} showWeek={agentReport} />}
      {agentReport && <AssigneeActivity owner={owner} project={project} revision={revision} />}
      {ready && <OwnerWork owner={owner} project={project} revision={revision} {...ready} />}
      {(reports.length > 0 || section === 'reports-pending-title') && (
        <section className="feed-section" aria-labelledby="reports-pending-title">
          <h2 id="reports-pending-title">Worker reports to review</h2>
          {reports.length > 0 ? <PendingReports owner={owner} project={project} name={name} initial={reports}
            total={entry.report_count} cursor={entry.pending_reports_before} heading={false} />
            : <p className="section-message">No worker reports to review.</p>}
        </section>
      )}
      {!hasWork && !showOutcomes && !reports.length && !section && (
        <p className="section-message">No current work or recent outcomes.</p>
      )}
    </>
  )
}
