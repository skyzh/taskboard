import React, { useState } from 'react'
import { request, useLoadedPages, useResource } from './data.js'
import { EvidenceLinks, LoadMore, ResourceStatus, TaskLink, eventHref, notificationLogProject, ownerHref, profileName, signInHref } from './shared.jsx'
import AttentionRequests from './AttentionRequests.jsx'
import NotificationLog from './NotificationLog.jsx'
import DecisionForm from './DecisionForm.jsx'
import { Markdown } from './Markdown.jsx'
import { TaskRows } from './WorkRow.jsx'
import { EventRow } from './Work.jsx'
import { TaskWaitAge } from './WaitAge.jsx'
import { useTimeZone } from './timeZone.js'

const relativeTime = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })

function createdAgo(at) {
  const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(at)) / 60_000))
  if (minutes === 0) return 'Just now'
  if (minutes < 60) return relativeTime.format(-minutes, 'minute')
  if (minutes < 1440) return relativeTime.format(-Math.floor(minutes / 60), 'hour')
  return relativeTime.format(-Math.floor(minutes / 1440), 'day')
}

function Creation({ event }) {
  const { formatTimestamp } = useTimeZone()
  const timestamp = formatTimestamp(event.at)
  const creator = profileName(event.creator)
  return (
    <li className="event-row">
      <p className="event-ticket"><TaskLink reference={event.task_key} id={event.task_id} title={event.task_title} /></p>
      <p className="event-meta">
        <span>Created by {creator}</span>
        <a href={eventHref(event.id)} aria-label={`View task creation by ${creator} at ${timestamp}`}>
          <time dateTime={event.at} title={timestamp}>{createdAgo(event.at)}</time>
        </a>
      </p>
    </li>
  )
}

function DecisionRequest({ task, current, session, onChanged, onAnswerStarted }) {
  return (
    <li className="event-row">
      <p className="event-ticket"><TaskLink reference={task.key} id={task.id} title={task.title} /></p>
      <p className="event-meta">
        <span>{current ? 'Decision requested' : 'Decision'}</span>
        {current && <TaskWaitAge task={task} href={eventHref(task.event_id)} />}
      </p>
      <Markdown text={task.note} />
      <EvidenceLinks links={task.links} />
      <DecisionForm task={current ? task : undefined} session={session}
        onChanged={onChanged} onAnswerStarted={onAnswerStarted} />
    </li>
  )
}

const readinessEventLabels = {
  merged: 'Merge recorded',
  deferred: 'Deferral recorded',
  completed: 'Completion recorded',
}

function InboxReadiness({ item }) {
  return (
    <li className="event-row">
      <p className="event-ticket"><TaskLink reference={item.key} id={item.id} title={item.title} /></p>
      <p>Check whether this task can move to Ready.</p>
      <p>{item.criteria}</p>
      <p className="event-meta">
        <TaskLink reference={item.source_key} id={item.source_id} title={item.source_title} />
        <a href={eventHref(item.event_id)}>{readinessEventLabels[item.event_kind] || 'Dependency updated'}</a>
      </p>
    </li>
  )
}

const workLabels = {
  ready: 'Ready to start',
  review: 'Independent review',
  report: 'Collect a worker report',
  in_progress: 'Continue work in progress',
  shared_ready: 'Unassigned Ready work',
}

function InboxWork({ items }) {
  if (items.length === 0) return null
  return (
    <ul className="event-list" aria-label="Next actionable work" role="list">
      {items.map(item => (
        <li className="event-row" key={item.kind}>
          <p className="event-ticket"><TaskLink id={item.id} reference={item.key} title={item.title} /></p>
          <p className="event-meta">{workLabels[item.kind]}</p>
        </li>
      ))}
    </ul>
  )
}

function InboxStandingGoals({ goals, owner }) {
  if (goals.length === 0) return null
  return (
    <ul className="event-list" aria-label="Standing goals" role="list">
      {goals.map(goal => (
        <li className="event-row dri-goal" key={goal.project}>
          <p className="event-ticket"><a href={ownerHref(owner, 'dri-standing-goal-title', goal.project)}>Standing goal</a></p>
          <p>{goal.summary}</p>
        </li>
      ))}
    </ul>
  )
}

function InboxEntries({ revision, owner }) {
  const page = useLoadedPages('/notifications/inbox', { scope: 'all', view: 'updates' }, revision)
  const work = useResource('/notifications/work?scope=all', revision)
  const items = page.data?.items ?? []
  const standingGoals = work.data?.standing_goals || []
  const actionable = (work.data?.items || []).filter(item => Object.hasOwn(workLabels, item.kind))

  return (
    <>
      <ResourceStatus resource={page} label="your inbox" />
      {work.error && work.error.status !== 404 && <ResourceStatus resource={work} label="next actionable work" />}
      <InboxWork items={actionable} />
      {page.data && !work.pending && items.length === 0 && actionable.length === 0 && standingGoals.length === 0 && (
        <p className="section-message">Your inbox is empty.</p>
      )}
      {items.length > 0 && (
        <ol className="event-list" aria-label="Inbox" role="list">
          {items.map(({ kind, id, item }) => {
            const key = `${kind}-${id}`
            if (kind === 'creation') return <Creation key={key} event={item} />
            if (kind === 'watched') return <EventRow key={key} event={item} showTask />
            if (kind === 'readiness') return <InboxReadiness key={key} item={item} />
            return null
          })}
        </ol>
      )}
      <LoadMore page={page} label="inbox items" />
      <InboxStandingGoals goals={standingGoals} owner={owner} />
    </>
  )
}

function Inbox({ revision, owner }) {
  const preference = useResource('/notifications/preferences', revision)
  const [saving, setSaving] = useState(false)
  const [failure, setFailure] = useState(null)

  async function subscribe(taskCreations) {
    setSaving(true)
    setFailure(null)
    try {
      await request('/notifications/preferences', { body: { task_creations: taskCreations } })
      preference.retry()
    } catch (error) {
      setFailure(error.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="feed-section" aria-label="Inbox">
      <ResourceStatus resource={preference} label="notification preferences" />
      {preference.data && (
        <label className="activity-mode">
          <input type="checkbox" checked={preference.data.task_creations} disabled={preference.pending || saving}
            onChange={event => subscribe(event.target.checked)} />
          Include new tasks
        </label>
      )}
      {failure && <p role="alert" className="section-message">{failure}</p>}
      {preference.data && (
        <InboxEntries revision={`${revision}:${preference.data.task_creations}`} owner={owner} />
      )}
    </section>
  )
}

function FollowThrough({ revision }) {
  const page = useLoadedPages('/notifications/decisions', { state: 'answered', scope: 'all' }, revision)
  if (!page.data?.items.length && !page.error) return null
  return (
    <>
      <h3 className="notification-subhead">Awaiting follow-through</h3>
      <ResourceStatus resource={page} label="answered decisions" />
      <ol className="event-list" role="list">
        {page.data?.items.map(task => (
          <li className="event-row" key={task.id}>
            <p><TaskLink reference={task.key} id={task.id} title={task.title} /></p>
            <Markdown text={task.note} />
            <p><a href={eventHref(task.event_id)}>Recorded decision</a></p>
            <EvidenceLinks links={task.links} />
          </li>
        ))}
      </ol>
      <LoadMore page={page} label="answered decisions" />
    </>
  )
}

function NeedsMe({ revision, session, onChanged }) {
  const decisions = useLoadedPages('/notifications/decisions', { scope: 'all' }, revision)
  const [submitted, setSubmitted] = useState([])
  const current = decisions.data?.items ?? []
  const currentIds = new Set(current.map(task => task.event_id))
  // Keep in-flight decision receipts when an answered request leaves the open list.
  const items = [...submitted.filter(task => !currentIds.has(task.event_id)), ...current]
  return (
    <section className="feed-section" aria-labelledby="attention-title">
      <h2 id="attention-title">Needs your attention</h2>
      <h3 className="notification-subhead">Decision requests</h3>
      <ResourceStatus resource={decisions} label="your decision requests" />
      {items.length > 0 && (
        <ol className="event-list" aria-label="Decision requests for you" role="list">
          {items.map(task => (
            <DecisionRequest key={task.event_id} task={task} current={currentIds.has(task.event_id)}
              session={session} onChanged={onChanged} onAnswerStarted={() => {
                setSubmitted(previous => previous.some(row => row.event_id === task.event_id)
                  ? previous : [...previous, task])
              }} />
          ))}
        </ol>
      )}
      {decisions.data && items.length === 0 && <p className="section-message">No open decision requests.</p>}
      <LoadMore page={decisions} label="open decision requests" />
      <AttentionRequests revision={revision} onChanged={onChanged} />
      <FollowThrough revision={revision} />
    </section>
  )
}

function TaskReminders({ revision }) {
  const reminders = useLoadedPages('/notifications/task-reminders', { scope: 'all' }, revision)
  const items = reminders.data?.items
  if (items?.length === 0 && !reminders.pending && !reminders.error) return null
  return (
    <section className="feed-section" aria-labelledby="reminders-title">
      <h2 id="reminders-title">Task reminders</h2>
      <ResourceStatus resource={reminders} label="task reminders" />
      <TaskRows items={items} label="Tasks to review" />
      <LoadMore page={reminders} label="task reminders" />
    </section>
  )
}

function PersonalNotifications({ revision, session, onChanged, fragment, project, projects }) {
  const needsMe = fragment === 'notifications-needs-me'
  const showLog = fragment === 'notifications-log' || Boolean(notificationLogProject(fragment))
  const { data } = useResource('/notifications/open-count?scope=all', revision)
  return (
    <>
      <nav className="notification-views" aria-label="Notification views">
        <a href="#notifications" aria-current={fragment === 'notifications' ? 'page' : undefined}>Inbox</a>
        <a href="#notifications-needs-me" aria-current={needsMe ? 'page' : undefined}>
          Needs me{data && <span aria-live="polite"> ({data.count})</span>}
        </a>
        <a href={showLog ? `#${fragment}` : '#notifications-log'} aria-current={showLog ? 'page' : undefined}>Project log</a>
      </nav>
      {showLog ? <NotificationLog project={project} projects={projects} revision={revision} /> : (
        <>
          <TaskReminders revision={revision} />
          {needsMe ? <NeedsMe revision={revision} session={session} onChanged={onChanged} />
            : <Inbox revision={revision} owner={session.data?.actor?.id} />}
        </>
      )}
    </>
  )
}

export default function Notifications({ session, verified, revision, onChanged, fragment, project, projects }) {
  const sessionFailed = session.error && session.error.status !== 401
  return (
    <>
      <div className="page-heading"><h1 id="notifications">Notifications</h1></div>
      {(session.pending || (!verified && sessionFailed)) && <ResourceStatus resource={session} label="your session" />}
      {verified && <PersonalNotifications key={session.data?.actor?.id ?? session.data?.actor?.name}
        revision={revision} session={session} onChanged={onChanged} fragment={fragment} project={project} projects={projects} />}
      {!verified && !session.pending && !sessionFailed && (
        <p className="section-message">
          {session.data?.sign_in_available || session.error?.status === 401
            ? <a href={signInHref(`#${fragment}`)}>Sign in with Raft to view your notifications</a>
            : 'Notifications require a verified Raft account.'}
        </p>
      )}
    </>
  )
}
