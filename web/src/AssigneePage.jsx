import React, { useState } from 'react'
import { BoardLink, useContentTitle } from './BoardNavigation.jsx'
import { queryPath, request, useLoadedPages, useResource } from './data.js'
import { ActivityDate, Outcomes } from './Overview.jsx'
import { NeedsYou, OwnerWorkLinks, PendingReports, ownerWorkSections } from './Reports.jsx'
import {
  ActivityMark, AssigneeAvatar, DriWorkStatus, LoadMore, ResourceStatus, TaskLink, Timestamp, eventHref, ownerHref, profileName,
  taskLabel, useSignInHref,
} from './shared.jsx'
import { TaskRows } from './WorkRow.jsx'
import DriWorkers from './DriWorkers.jsx'
import { NotificationLogEntry, wakeReason } from './NotificationLog.jsx'
import ProjectPicker from './ProjectPicker.jsx'
import DriGoal, { DriMilestones } from './DriGoal.jsx'
import { Markdown, MarkdownInline } from './Markdown.jsx'
import { useTimeZone } from './timeZone.js'
import { activityHeadline, briefSourceText } from './workCopy.js'

function OwnerWork({ owner, state, id, title, label, revision, scope, projectNames }) {
  const page = useLoadedPages('/tasks', { dri: owner, state, ...scope, order: state === 'ready' ? 'priority' : undefined }, revision)
  return (
    <section className="feed-section" aria-labelledby={id}>
      <h2 id={id}>{title}</h2>
      <ResourceStatus resource={page} label={label} />
      <TaskRows items={page.data?.items} owner={false} label={title} report revision={revision} projectNames={projectNames} />
      {page.data?.items.length === 0 && <p className="section-message">No {label}.</p>}
      <LoadMore page={page} label={label} />
    </section>
  )
}

function AssigneeActivity({ owner, scope, revision }) {
  const { formatDay } = useTimeZone()
  const page = useLoadedPages('/activity', { actor: owner, ...scope, detail: 'reports' }, revision, { limit: 10 })
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
      {page.data?.items.length === 0 && <p className="section-message">No reported activity.</p>}
      {[...days].sort(([left], [right]) => right.localeCompare(left)).map(([day, events]) => (
        <div className="outcome-day" key={day}>
          <h3><ActivityDate day={day} /></h3>
          <ul className="assignee-activity-list" role="list">
            {events.map(event => {
              const recorded = activityHeadline(event)
              const headline = recorded && recorded !== event.label ? recorded : event.task_title || event.label
              return (
                <li key={event.id}>
                  <BoardLink href={eventHref(event.id)}>
                    <ActivityMark event={event} />
                    <span>
                      <span className="assignee-activity-headline"><MarkdownInline text={headline} links={false} /></span>
                      {' '}<span className="assignee-activity-meta">
                        {taskLabel(event.task_id, event.task_key)} · <Timestamp at={event.at} showDate={false} />
                      </span>
                    </span>
                  </BoardLink>
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

function PendingReviews({ owner, scope, revision }) {
  const page = useLoadedPages('/assignee/reviews', { owner, ...scope }, revision, { limit: 10 })
  const items = page.data?.items ?? []
  return (
    <section className="feed-section" aria-labelledby="assignee-pending-reviews">
      <h2 id="assignee-pending-reviews">Pending reviews</h2>
      <ResourceStatus resource={page} label="pending reviews" />
      {page.data && !page.pending && !page.error && items.length === 0 && (
        <p className="section-message">No pending reviews for the selected projects.</p>
      )}
      {items.length > 0 && <ol className="event-list" role="list">
        {items.map(item => <li className="event-row" key={item.id}>
          <p className="event-ticket">
            <TaskLink id={item.source_id} reference={item.request_key || item.source_key}
              title={item.request_title || item.source_title} />
          </p>
          <p className="event-meta">
            {item.phase === 'asked' ? 'Awaiting pickup' : item.phase === 'in_progress' ? 'In progress' : 'Start unconfirmed'}
          </p>
        </li>)}
      </ol>}
      <LoadMore page={page} label="pending reviews" />
    </section>
  )
}

function WakeHistory({ owner, scope, revision }) {
  const page = useLoadedPages('/activity', { action: 'wake', actor: owner, ...scope }, revision, { limit: 10 })
  const items = page.data?.items ?? []
  return (
    <section className="feed-section" aria-labelledby="assignee-wake-history">
      <h2 id="assignee-wake-history">Wake history</h2>
      {items[0] && <p>Latest wake reason: {wakeReason(items[0])}{' · '}
        <TaskLink id={items[0].task_id} reference={items[0].task} />
      </p>}
      <ResourceStatus resource={page} label="wake history" />
      {page.data && !page.pending && !page.error && items.length === 0 && (
        <p className="section-message">No wake history for the selected projects.</p>
      )}
      {items.length > 0 && <ol className="event-list" role="list">
        {items.map(item => <NotificationLogEntry key={item.id} item={item} />)}
      </ol>}
      <LoadMore page={page} label="wake history" />
    </section>
  )
}

function AssigneeWatch({ owner, scope, watching, onChanged }) {
  const [saving, setSaving] = useState(false)
  const [failure, setFailure] = useState(null)

  async function toggle() {
    if (saving) return
    setSaving(true)
    setFailure(null)
    try {
      await request(queryPath('/assignee/watch', { owner, ...scope }), { body: { watch: !watching } })
      onChanged()
    } catch (error) {
      setFailure(error.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="assignee-watch">
      <button type="button" aria-disabled={saving} onClick={toggle}>
        {watching ? 'Stop watching' : 'Watch'}
      </button>
      {failure && <p role="alert">{failure}</p>}
    </div>
  )
}

function ProjectGoals({ entries, owner, names }) {
  return (
    <section className="feed-section" aria-labelledby="profile-project-goals">
      <h2 id="profile-project-goals">Goals by project</h2>
      {entries.map(({ project, goal, standing_goal: standing, milestones }) => (
        <section key={project} aria-labelledby={`profile-goals-${project}`}>
          <h3 id={`profile-goals-${project}`}><BoardLink href={ownerHref(owner, undefined, project)}>
            {names.get(project) || project}
          </BoardLink></h3>
          {standing && <DriGoal goal={standing} project={project} standing
            headingId={`standing-goal-${project}`} heading="h4" />}
          {goal && <DriGoal goal={goal} project={project} headingId={`dated-goal-${project}`} heading="h4" />}
          {milestones.length > 0 && <DriMilestones milestones={milestones} project={project}
            headingId={`milestones-${project}`} heading="h4" itemHeading="h5" />}
        </section>
      ))}
    </section>
  )
}

export default function AssigneePage({ owner, section, revision, project, allProjects = false, projects = [], onProjectChange,
  reportsHref = '#reports', canEditGoal, viewer, onWatchChanged }) {
  const scope = allProjects ? { scope: 'all' } : { project }
  const routeProject = allProjects ? 'all' : project
  const resource = useResource(queryPath('/assignee', { owner, ...scope }), revision)
  const entry = resource.data
  const login = useSignInHref(ownerHref(owner, section, routeProject))
  useContentTitle(entry?.profile ? `${profileName(owner, entry.profile)} · Report` : null)
  if (!entry && resource.error?.status === 401) return <p className="section-message">
    <a href={login}>Sign in to view this agent report</a>
  </p>
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
  const projectGoals = entry.project_goals || []
  const choices = projects.filter(item => entry.projects?.includes(item.prefix))
  const projectNames = new Map(choices.map(item => [item.prefix, item.title]))
  const showMilestones = milestones.length > 0 || canEditGoal
  const showStandingGoal = Boolean(entry.standing_goal || canEditGoal)
  const counts = entry.task_counts
  const hasWork = !counts || ownerWorkSections.some(({ state }) => counts[state] > 0)
  const sections = ownerWorkSections.filter(({ state, id }) => counts?.[state] !== 0 || section === id)
  const ready = sections.find(({ state }) => state === 'ready')
  return (
    <>
      <BoardLink className="back-link" href={reportsHref}>Back to Reports</BoardLink>
      <header id="assignee-title" className="assignee-page-heading">
        <AssigneeAvatar name={name} url={profile?.avatar_url} large />
        <div>
          <div className="dri-name-line">
            <h1>{name}<DriWorkStatus inProgress={entry.in_progress} /></h1>
            <DriWorkers owner={owner} project={project} allProjects={allProjects} counts={entry.worker_counts} revision={revision} />
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
      {agentReport && viewer && choices.length > 1 && (
        <ProjectPicker projects={choices} value={routeProject} allProjects onChange={onProjectChange} />
      )}
      {viewer && typeof entry.watching === 'boolean' && (
        <AssigneeWatch owner={owner} scope={scope} watching={entry.watching}
          onChanged={onWatchChanged} />
      )}
      {viewer?.kind === 'human' && viewer.id === owner && <NeedsYou project={project} revision={revision} />}
      {(showStandingGoal || showMilestones || entry.goal || projectGoals.length > 0 || hasWork || hasOutcomeLink || reports.length > 0) && (
        <nav className="owner-links profile-index" aria-label={`${name}’s work sections`}>
          {showStandingGoal && <BoardLink href={ownerHref(owner, 'dri-standing-goal-title', routeProject)}>Standing goal</BoardLink>}
          {entry.goal && <BoardLink href={ownerHref(owner, 'dri-goal-title', routeProject)}>Original dated goal</BoardLink>}
          {showMilestones && <BoardLink href={ownerHref(owner, 'dri-milestones-title', routeProject)}>Dated milestones</BoardLink>}
          {projectGoals.length > 0 && <BoardLink href={ownerHref(owner, 'profile-project-goals', routeProject)}>Goals by project</BoardLink>}
          <OwnerWorkLinks owner={owner} project={routeProject} counts={counts} />
          {hasOutcomeLink && <BoardLink href={ownerHref(owner, 'outcomes-title', routeProject)}>Recent outcomes</BoardLink>}
          {agentReport && <BoardLink href={ownerHref(owner, 'assignee-activity-title', routeProject)}>What was done</BoardLink>}
          {agentReport && <BoardLink href={ownerHref(owner, 'assignee-pending-reviews', routeProject)}>Pending reviews</BoardLink>}
          {agentReport && <BoardLink href={ownerHref(owner, 'assignee-wake-history', routeProject)}>Wake history</BoardLink>}
          {reports.length > 0 && (
            <BoardLink href={ownerHref(owner, 'reports-pending-title', routeProject)}>Worker reports <strong>{entry.report_count}</strong></BoardLink>
          )}
        </nav>
      )}
      {projectGoals.length > 0 && <ProjectGoals entries={projectGoals} owner={owner} names={projectNames} />}
      {showStandingGoal && <DriGoal goal={entry.standing_goal} project={project} standing editable={canEditGoal} onChanged={resource.retry} />}
      {entry.goal && <DriGoal goal={entry.goal} project={project} editable={canEditGoal} onChanged={resource.retry} />}
      {showMilestones && <DriMilestones milestones={milestones} project={project} editable={canEditGoal} onChanged={resource.retry} />}
      {sections.filter(item => item.state !== 'ready').map(item =>
        <OwnerWork key={item.state} owner={owner} scope={scope} projectNames={allProjects ? projectNames : undefined} revision={revision} {...item} />)}
      {showOutcomes && <Outcomes revision={revision} owner={owner} project={project} allProjects={allProjects} showWeek={agentReport} />}
      {agentReport && <AssigneeActivity owner={owner} scope={scope} revision={revision} />}

      {ready && <OwnerWork owner={owner} scope={scope} projectNames={allProjects ? projectNames : undefined} revision={revision} {...ready} />}
      {agentReport && <PendingReviews owner={owner} scope={scope} revision={revision} />}
      {agentReport && <WakeHistory owner={owner} scope={scope} revision={revision} />}
      {(reports.length > 0 || section === 'reports-pending-title') && (
        <section className="feed-section" aria-labelledby="reports-pending-title">
          <h2 id="reports-pending-title">Worker reports to review</h2>
          {reports.length > 0 ? <PendingReports owner={owner} project={project} name={name} initial={reports}
            total={entry.report_count} cursor={entry.pending_reports_before} heading={false}
            reportFilters={allProjects ? { ...scope, view: 'profile' } : undefined} />
            : <p className="section-message">No worker reports to review.</p>}
        </section>
      )}
      {!hasWork && !showOutcomes && !reports.length && !section && (
        <p className="section-message">No current work or recent outcomes.</p>
      )}
    </>
  )
}
