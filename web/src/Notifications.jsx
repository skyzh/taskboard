import React, { useState } from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { queryPath, useLoadedPages, useResource } from './data.js'
import { EvidenceLinks, LoadMore, ResourceStatus, TaskLink, eventHref, notificationLogFilters, ownerHref, profileName, useSignInHref, taskHref } from './shared.jsx'
import AttentionRequests from './AttentionRequests.jsx'
import NotificationLog from './NotificationLog.jsx'
import ReminderDate from './ReminderDate.jsx'
import DecisionForm, { mayAnswer } from './DecisionForm.jsx'
import DecisionReceiptActions, { decisionTicketHref } from './DecisionReceiptActions.jsx'
import BrowserPush from './BrowserPush.jsx'
import { GoalDeadline } from './DriGoal.jsx'
import GoalOccurrences from './GoalOccurrences.jsx'
import { WorkflowPrompt } from './FieldGuidance.jsx'
import { Markdown } from './Markdown.jsx'
import { TaskRows } from './WorkRow.jsx'
import { EventRow } from './Work.jsx'
import { TaskWaitAge } from './WaitAge.jsx'
import { ReportTime } from './ReportTime.jsx'
import TaskOwnership from './TaskOwnership.jsx'
import TransitionFeedback, { useTransitionRequest } from './TransitionFeedback.jsx'
import { projectFilters, projectHref } from './projectScope.js'

function DecisionRequest({ task, current, session, onChanged, onAnswerStarted, project, revision, canDefer }) {
  return (
    <li className="event-row">
      <p className="event-ticket"><TaskLink reference={task.key} id={task.id} title={task.title}
        publicHref={decisionTicketHref(task, project)} /></p>
      <p className="event-meta">
        <span>{current ? 'Decision requested' : 'Decision'}</span>
        {current && <TaskWaitAge task={task} href={eventHref(task.event_id)} />}
      </p>
      <Markdown text={task.note} />
      <EvidenceLinks links={task.links} />
      {task.remind_on && <p className="event-meta">Reminder <time dateTime={task.remind_on}>{task.remind_on}</time> (Pacific time)</p>}
      <DecisionForm task={current ? task : undefined} session={session}
        onChanged={onChanged} onAnswerStarted={onAnswerStarted}
        receiptAction={<DecisionReceiptActions project={project} revision={revision} />} />
      {current && canDefer && task.decision_request_id && mayAnswer(task, session) && (
        <ReminderDate key={task.remind_on || ''}
          path={`/notifications/decisions/${encodeURIComponent(task.key)}/${task.decision_request_id}/reminder`}
          saved={task.remind_on} deferAsk onChanged={onChanged} />
      )}
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
        <BoardLink href={eventHref(item.event_id)}>{readinessEventLabels[item.event_kind] || 'Dependency updated'}</BoardLink>
      </p>
    </li>
  )
}

const workLabels = {
  ready: 'Ready to start',
  review: 'Independent review',
  report: 'Collect a worker report',
  goal_review: 'Goal review due',
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

function InboxDatedGoals({ goals }) {
  if (goals.length === 0) return null
  return (
    <ul className="event-list" aria-label="Dated goals" role="list">
      {goals.map(goal => (
        <li className="event-row dri-goal" key={goal.ticket}>
          <p className="event-ticket"><TaskLink reference={goal.ticket} title={goal.summary} /></p>
          <p>Due <GoalDeadline deadline={goal.deadline} /></p>
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
          <p className="event-ticket"><BoardLink href={goal.ticket ? taskHref(goal.ticket) : ownerHref(owner, 'dri-standing-goal-title', goal.project)}>Standing goal</BoardLink></p>
          <p>{goal.summary}</p>
        </li>
      ))}
    </ul>
  )
}

const watchedAckPreference = '/notifications/preferences/watched-ack'

function WatchedUpdate({ row, canAcknowledge, revision, onChanged }) {
  const mutation = useTransitionRequest(onChanged)
  return (
    <EventRow event={row.item} showTask>
      {canAcknowledge && <button type="button" disabled={mutation.saving}
        onClick={() => mutation.submit(`/notifications/watched/${row.id}/acknowledge`, {})}>
        {mutation.saving ? 'Acknowledging…' : 'Acknowledge'}
      </button>}
      <TransitionFeedback mutation={mutation} />
      {row.reopen && <TaskOwnership task={row.reopen.task} actions={['reopen']} project={row.reopen.project}
        revision={revision} canAssignOnReopen={row.reopen.can_assign} onChanged={onChanged} />}
    </EventRow>
  )
}

function WatchedUpdatePreference({ preference, history, setHistory, onChanged }) {
  const mutation = useTransitionRequest(onChanged)
  if (preference.error?.status === 404) return null
  return (
    <div className="notification-filters">
      <ResourceStatus resource={preference} label="watched update preference" />
      {preference.data && <>
        <label>
          <input type="checkbox" checked={preference.data.enabled}
            disabled={preference.pending || mutation.saving}
            onChange={event => mutation.submit(watchedAckPreference, { enabled: event.currentTarget.checked })} />
          Show Acknowledge on watched updates
        </label>
        <button type="button" onClick={() => setHistory(!history)}>
          {history ? 'Back to Inbox updates' : 'Acknowledged updates'}
        </button>
        <TransitionFeedback mutation={mutation} />
      </>}
    </div>
  )
}

function InboxEntries({ revision, owner, project, openCount, onChanged }) {
  const [history, setHistory] = useState(false)
  const preference = useResource(watchedAckPreference, revision)
  const page = useLoadedPages('/notifications/inbox', {
    ...projectFilters(project), view: history ? 'acknowledged' : 'updates',
  }, revision)
  const work = useResource(queryPath('/notifications/work', { ...projectFilters(project), refresh_guidance: 'true' }), revision)
  const items = (page.data?.items ?? []).filter(row => row.kind === 'watched' || row.kind === 'readiness')
  const datedGoals = work.data?.dated_goals || []
  const standingGoals = work.data?.standing_goals || []
  const occurrences = work.data?.goal_occurrences || []
  const reviewTickets = new Set(occurrences.filter(item => item.kind === 'review').map(item => item.ticket))
  const actionable = (work.data?.items || []).filter(item => Object.hasOwn(workLabels, item.kind)
    && (item.kind !== 'goal_review' || !reviewTickets.has(item.key)))
  const legacyWorkEndpoint = work.error?.status === 404
  const showEmpty = page.data && !page.pending && !page.error
    && !work.pending && (work.data || legacyWorkEndpoint)
    && !openCount.pending && !openCount.error && openCount.data?.count === 0
    && items.length === 0 && actionable.length === 0 && datedGoals.length === 0 && standingGoals.length === 0
    && occurrences.length === 0

  return (
    <>
      <ResourceStatus resource={page} label="your inbox" />
      {openCount.error && <ResourceStatus resource={openCount} label="Inbox action count" />}
      {work.error && !legacyWorkEndpoint && <ResourceStatus resource={work} label="next actionable work" />}
      <InboxWork items={actionable} />
      {work.data?.goal_review_prompt && !reviewTickets.size && <>
        <h3>Goal review</h3>
        <Markdown text={work.data.goal_review_prompt.prompt} />
      </>}
      <WorkflowPrompt entry={work.data?.workflow_prompt} />
      {work.data?.inbox_refresh && <>
        <p className="section-message">{work.data.inbox_refresh}</p>
        <Markdown text={work.data.inbox_follow_up?.prompt} />
      </>}
      <WatchedUpdatePreference preference={preference} history={history} setHistory={setHistory} onChanged={onChanged} />
      {showEmpty && !history && <p className="section-message">Your inbox is empty.</p>}
      {history && page.data && !page.pending && !page.error && items.length === 0 &&
        <p className="section-message">No acknowledged watched updates.</p>}
      {items.length > 0 && (
        <ol className="event-list" aria-label={history ? 'Acknowledged watched updates' : 'Inbox'} role="list">
          {items.map(row => row.kind === 'watched' ? (
            <WatchedUpdate key={`watched-${row.id}`} row={row} revision={revision} onChanged={onChanged}
              canAcknowledge={!history && preference.data?.enabled} />
          ) : <InboxReadiness key={`readiness-${row.id}`} item={row.item} />)}
        </ol>
      )}
      <LoadMore page={page} label="inbox items" />
      <GoalOccurrences key={`${owner}:${project || 'all'}`} items={occurrences} onChanged={onChanged} showTicket />
      <InboxDatedGoals goals={datedGoals} />
      <InboxStandingGoals goals={standingGoals} owner={owner} />
      {work.data?.standing_goal_guidance && <p className="section-message">{work.data.standing_goal_guidance}</p>}
    </>
  )
}

function Inbox({ revision, session, project, openCount, onChanged }) {
  return (
    <section className="feed-section" aria-label="Inbox">
      <BoardLink href={projectHref('#activity?action=add', project || 'all')}>Task created activity</BoardLink>
      <InboxEntries revision={revision} owner={session.data?.actor?.id} project={project} openCount={openCount} onChanged={onChanged} />
      <NeedsMe key={project} revision={revision} session={session} onChanged={onChanged} project={project} onlyOpen />
    </section>
  )
}

function FollowThrough({ revision, project }) {
  const page = useLoadedPages('/notifications/decisions', { state: 'answered', ...projectFilters(project) }, revision)
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
            <p><BoardLink href={eventHref(task.event_id)}>Recorded decision</BoardLink></p>
            <EvidenceLinks links={task.links} />
          </li>
        ))}
      </ol>
      <LoadMore page={page} label="answered decisions" />
    </>
  )
}

function AskHistory({ revision, project }) {
  const page = useLoadedPages('/notifications/ask-history', projectFilters(project), revision)
  const items = page.data?.items ?? []
  return (
    <section aria-labelledby="ask-history-title">
      <h3 className="notification-subhead" id="ask-history-title">Ask history</h3>
      <ResourceStatus resource={page} label="ask history" />
      {page.data && !page.pending && !page.error && items.length === 0 &&
        <p className="section-message">No answered decisions or handled requests.</p>}
      {items.length > 0 && (
        <ol className="event-list" aria-label="Ask history" role="list">
          {items.map(item => {
            const decision = item.kind === 'decision'
            const source = decision
              ? `${item.source}&decision-scope=${encodeURIComponent(project || 'all')}`
              : item.source
            return (
              <li className="event-row" key={`${item.kind}-${item.request_id}`}>
                <p className="event-ticket"><BoardLink href={source}>{item.ticket} · {item.title}</BoardLink></p>
                <p className="event-meta">
                  <span>{decision ? 'Decision answered' : 'Request handled'}</span>
                  <ReportTime at={item.at} />
                </p>
                <Markdown text={item.question} />
                {decision && (
                  <div>
                    {item.choice && item.choice !== item.outcome && !item.outcome?.startsWith(`${item.choice} · `) &&
                      <p>{item.choice}</p>}
                    <Markdown text={item.outcome} />
                  </div>
                )}
              </li>
            )
          })}
        </ol>
      )}
      <LoadMore page={page} label="ask history" />
    </section>
  )
}

function ReviewRequests({ revision, project }) {
  const page = useLoadedPages('/notifications/needs-you', { ...projectFilters(project), kind: 'review' }, revision)
  const reviews = page.data?.items
  if ((!page.error && !reviews?.length) || page.error?.status === 404) return null
  return (
    <section aria-labelledby="review-requests-title">
      <h3 className="notification-subhead" id="review-requests-title">Review requests</h3>
      <ResourceStatus resource={page} label="your review requests" />
      {reviews?.length > 0 && (
        <ul className="event-list" aria-label="Review requests for you" role="list">
          {reviews.map(item => (
            <li className="event-row" key={item.id}>
              <p className="event-ticket"><TaskLink reference={item.ticket} title={item.title} /></p>
              <p className="event-meta">
                <span>{profileName(item.requester, item.requester_profile)}</span>
                {item.asked_at && <span>Asked <ReportTime at={item.asked_at} linked /></span>}
              </p>
            </li>
          ))}
        </ul>
      )}
      <LoadMore page={page} label="review requests" />
    </section>
  )
}

function decisionAskKey(task) {
  return task.decision_request_id ?? task.event_id
}

function PendingAsks({ revision, session, onChanged, project, onlyOpen, state }) {
  const filters = projectFilters(project)
  const decisions = useLoadedPages('/notifications/decisions', state === 'open' ? filters : { ...filters, state }, revision)
  const [submitted, setSubmitted] = useState([])
  const current = decisions.data?.items ?? []
  const currentIds = new Set(current.map(decisionAskKey))
  // Keep in-flight decision receipts when an answered request leaves the open list.
  const items = [...submitted.filter(task => !currentIds.has(decisionAskKey(task))), ...current]
  return (
    <>
      {onlyOpen && items.length > 0 && <h2 id="attention-title">Needs your attention</h2>}
      {(!onlyOpen || items.length > 0) && <h3 className="notification-subhead">{state === 'deferred' ? 'Deferred decision requests' : 'Decision requests'}</h3>}
      <ResourceStatus resource={decisions} label={`your ${state} decision requests`} />
      {items.length > 0 && (
        <ol className="event-list" aria-label={state === 'deferred' ? 'Deferred decision requests for you' : 'Decision requests for you'} role="list">
          {items.map(task => (
            <DecisionRequest key={decisionAskKey(task)} task={task} current={currentIds.has(decisionAskKey(task))}
              canDefer={!onlyOpen}
              session={session} project={project} revision={revision} onChanged={onChanged} onAnswerStarted={() => {
                setSubmitted(previous => previous.some(row => decisionAskKey(row) === decisionAskKey(task))
                  ? previous : [...previous, task])
              }} />
          ))}
        </ol>
      )}
      {!onlyOpen && decisions.data && items.length === 0 && <p className="section-message">No {state} decision requests.</p>}
      <LoadMore page={decisions} label={`${state} decision requests`} />
      <AttentionRequests revision={revision} onChanged={onChanged} project={project} onlyOpen={onlyOpen}
        askState={onlyOpen ? undefined : state} />
    </>
  )
}

function NeedsMe({ revision, session, onChanged, project, onlyOpen = false }) {
  const [filter, setFilter] = useState('open')
  const states = filter === 'all' ? ['open', 'deferred'] : [filter]
  return (
    <section className="feed-section" aria-label={onlyOpen ? "Open Inbox requests" : undefined}
      aria-labelledby={onlyOpen ? undefined : "attention-title"}>
      {!onlyOpen && <>
        <h2 id="attention-title">Needs your attention</h2>
        <label>Pending asks <select value={filter} onChange={event => setFilter(event.target.value)}>
          <option value="open">Open</option>
          <option value="deferred">Deferred</option>
          <option value="all">Open and deferred</option>
        </select></label>
      </>}
      {states.map(state => <PendingAsks key={state} state={state} revision={revision} session={session}
        onChanged={onChanged} project={project} onlyOpen={onlyOpen} />)}
      <ReviewRequests revision={revision} project={project} />
      {!onlyOpen && <FollowThrough revision={revision} project={project} />}
      {!onlyOpen && <AskHistory revision={revision} project={project} />}
    </section>
  )
}

function TaskReminders({ revision, project }) {
  const reminders = useLoadedPages('/notifications/task-reminders', projectFilters(project), revision)
  const items = reminders.data?.items
  if (!items?.length && !reminders.error) {
    return reminders.pending && !reminders.data
      ? <span className="visually-hidden" role="status">Loading task reminders…</span>
      : null
  }
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
  const [route, query] = fragment.split('?', 2)
  const needsMe = route === 'notifications-needs-me'
  const scope = project || (new URLSearchParams(query).get('scope') === 'all' ? 'all' : undefined)
  const logFilters = notificationLogFilters(fragment)
  const openCount = useResource(queryPath('/notifications/open-count', projectFilters(project)), revision)
  return (
    <>
      <nav className="notification-views" aria-label="Notification views">
        <BoardLink href={projectHref('#notifications', scope)} aria-current={route === 'notifications' ? 'page' : undefined}>Inbox</BoardLink>
        <BoardLink href={projectHref('#notifications-needs-me', scope)} aria-current={needsMe ? 'page' : undefined}>
          Needs me{openCount.data && <span aria-live="polite"> ({openCount.data.count})</span>}
        </BoardLink>
        <BoardLink href={projectHref('#activity?action=wake&wake_status=pending', project || 'all')}>Pending notifications</BoardLink>
      </nav>
      {logFilters ? <>
        <BoardLink href={projectHref('#activity?action=wake', project || 'all')}>Wake activity</BoardLink>
        <NotificationLog project={project} state={logFilters.state} kind={logFilters.kind} agent={logFilters.agent}
          allProjects={logFilters.allProjects} projects={projects} revision={revision} />
      </> : (
        <>
          {session.data?.actor?.kind === 'human' && <BrowserPush />}
          <TaskReminders revision={revision} project={project} />
          {needsMe ? <NeedsMe key={project} revision={revision} session={session} onChanged={onChanged} project={project} />
            : <Inbox revision={revision} session={session} project={project} openCount={openCount} onChanged={onChanged} />}
        </>
      )}
    </>
  )
}

export default function Notifications({ session, verified, revision, onChanged, fragment = 'notifications', project, projectPicker, projects }) {
  const login = useSignInHref(`#${fragment}`)
  const sessionFailed = session.error && session.error.status !== 401
  return (
    <>
      <div className="page-heading"><h1 id="notifications">Notifications</h1>
        {!notificationLogFilters(fragment) && projectPicker}
      </div>
      {(session.pending || (!verified && sessionFailed)) && <ResourceStatus resource={session} label="your session" />}
      {verified && <PersonalNotifications key={session.data?.actor?.id ?? session.data?.actor?.name}
        revision={revision} session={session} onChanged={onChanged} fragment={fragment} project={project} projects={projects} />}
      {!verified && !session.pending && !sessionFailed && (
        <p className="section-message">
          {session.data?.sign_in_available || session.error?.status === 401
            ? <a href={login}>Sign in with Raft to view your notifications</a>
            : 'Notifications require a verified Raft account.'}
        </p>
      )}
    </>
  )
}
