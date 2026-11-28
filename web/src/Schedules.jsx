import React, { useState } from 'react'
import { BoardLink, useContentTitle } from './BoardNavigation.jsx'
import { queryPath, request, useResource } from './data.js'
import { ResourceStatus, TaskLink, scheduleHref, schedulesHref, useSignInHref, taskHref, taskLabel } from './shared.jsx'
import { MarkdownInline } from './Markdown.jsx'
import { useTimeZone } from './timeZone.js'
import { RecurringRunList } from './RecurringRuns.jsx'

const scheduleStates = { active: 'Active', paused: 'Paused', canceled: 'Canceled' }
const runStates = {
  pending: 'Pending',
  missed: 'Missed',
  completed_on_time: 'Completed on time',
  completed_late: 'Completed late',
}

function asDeviceDate(value, precision = 16) {
  if (!value) return ''
  const date = new Date(value)
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, precision)
}

function ScheduleDate({ value }) {
  const { formatTimestamp } = useTimeZone()
  return value ? <time dateTime={value}>{formatTimestamp(value)}</time> : '—'
}

export function ScheduleSummary({ schedule, project, showOwner = true, showTicket = true, showNextRun = true }) {
  const { owner_name: owner, cadence_hours: cadence, state, next_at: next } = schedule
  const recurringTicket = schedule.reusable_ticket
  const interval = cadence % 24 === 0
    ? cadence === 24 ? 'Daily' : `Every ${cadence / 24} days`
    : cadence === 1 ? 'Hourly' : `Every ${cadence} hours`
  return (
    <dl className="schedule-summary">
      {showOwner && <div><dt>Owner</dt><dd>{owner || 'Unavailable assignee'}</dd></div>}
      {project && <div><dt>Project</dt><dd>{project}</dd></div>}
      <div><dt>Cadence</dt><dd>{interval}</dd></div>
      <div><dt>Status</dt><dd>{scheduleStates[state]}</dd></div>
      {showNextRun && state === 'active' && <div><dt>Next run</dt><dd><ScheduleDate value={next} /></dd></div>}
      {recurringTicket && <>
        {showTicket && <div><dt>Recurring ticket</dt><dd><TaskLink id={recurringTicket.id} reference={recurringTicket.key} /></dd></div>}
        <div><dt>Last reviewed</dt><dd>{schedule.last_reviewed_at
          ? <ScheduleDate value={schedule.last_reviewed_at} /> : 'Not yet reviewed'}</dd></div>
      </>}
    </dl>
  )
}

export function OccurrenceHistory({ occurrences }) {
  if (!occurrences?.length) return <p className="schedule-muted">No runs recorded yet.</p>
  return (
    <details className="schedule-history">
      <summary>Recent runs · {occurrences.length}</summary>
      <ul>
        {occurrences.map(run => (
          <li key={run.due_at}>
            <ScheduleDate value={run.due_at} />
            <span>{runStates[run.status]}</span>
            {run.task_id != null ? (
              <BoardLink href={taskHref(run.task_id, run.task_key)}>{taskLabel(run.task_id, run.task_key)}</BoardLink>
            ) : <span className="schedule-muted">No ticket issued</span>}
          </li>
        ))}
      </ul>
    </details>
  )
}

function useScheduleSave(path, onChanged) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  async function save(body) {
    setPending(true)
    setError('')
    try {
      await request(path, { body })
      onChanged()
    } catch (failure) {
      setError(failure.message)
    } finally {
      setPending(false)
    }
  }
  return { pending, error, save }
}

function RecurringControls({ schedule, onChanged }) {
  const { pending, error, save } = useScheduleSave(`/schedules/${encodeURIComponent(schedule.id)}/state`, onChanged)
  const [next, setNext] = useState('')
  const [confirmCancel, setConfirmCancel] = useState(false)
  if (schedule.state === 'canceled') return null
  return (
    <div className="schedule-actions" data-reload-blocked={pending || Boolean(next) || confirmCancel}>
      {schedule.state === 'active' ? (
        <button type="button" disabled={pending} onClick={() => save({ change: 'pause' })}>Pause</button>
      ) : (
        <form onSubmit={event => {
          event.preventDefault()
          save({ change: 'resume', next_at: new Date(next).toISOString() })
        }}>
          <label>Resume at (device time)
            <input type="datetime-local" value={next} disabled={pending} required onChange={event => setNext(event.target.value)} />
          </label>
          <button type="submit" disabled={pending}>Resume</button>
        </form>
      )}
      {confirmCancel ? (
        <div className="schedule-actions">
          <button type="button" disabled={pending} onClick={() => save({ change: 'cancel' })}>Confirm cancellation</button>
          <button type="button" disabled={pending} onClick={() => setConfirmCancel(false)}>Keep schedule</button>
        </div>
      ) : <button type="button" disabled={pending} onClick={() => setConfirmCancel(true)}>Cancel schedule</button>}
      {error && <p role="alert">{error}</p>}
    </div>
  )
}

function MaintenanceControls({ project, schedule, owners, onChanged }) {
  const initialNext = asDeviceDate(schedule.next_at)
  const initialMembers = schedule.members || []
  const [owner, setOwner] = useState(schedule.owner)
  const [hours, setHours] = useState(String(schedule.cadence_hours))
  const [next, setNext] = useState(initialNext)
  const [state, setState] = useState(schedule.state)
  const [members, setMembers] = useState(initialMembers)
  const { pending, error, save } = useScheduleSave('/schedules/maintenance', onChanged)
  const available = new Map(owners.map(option => [option.id, option.name]))
  for (const id of [schedule.owner, ...initialMembers]) {
    if (!available.has(id)) available.set(id, 'Unavailable assignee')
  }
  const membersChanged = members.length !== initialMembers.length || members.some(id => !initialMembers.includes(id))
  const changes = {}
  if (owner !== schedule.owner) changes.owner = owner
  if (hours !== String(schedule.cadence_hours)) changes.cadence_hours = Number(hours)
  if (next !== initialNext && next) changes.next_at = new Date(next).toISOString()
  if (state !== schedule.state) changes.state = state
  if (membersChanged) changes.members = members
  const dirty = Object.keys(changes).length > 0
  return (
    <details className="schedule-editor">
      <summary>Edit maintenance schedule</summary>
      <form onSubmit={event => {
        event.preventDefault()
        if (dirty) save({ project, ...changes })
      }} data-reload-blocked={pending || dirty}>
        <fieldset disabled={pending}>
          <label>Owner
            <select value={owner} onChange={event => setOwner(event.target.value)}>
              {[...available].map(([id, name]) => <option key={id} value={id}>{name}</option>)}
            </select>
          </label>
          <label>Cadence in hours
            <input type="number" min="1" step="1" required value={hours} onChange={event => setHours(event.target.value)} />
          </label>
          <label>Next run (device time)
            <input type="datetime-local" required value={next} onChange={event => setNext(event.target.value)} />
          </label>
          <label>Status
            <select value={state} onChange={event => setState(event.target.value)}>
              <option value="active">Active</option>
              <option value="paused">Paused</option>
            </select>
          </label>
          <label>Expected owners (optional)
            <select multiple value={members} size={Math.min(Math.max(available.size, 2), 6)}
              onChange={event => setMembers(Array.from(event.target.selectedOptions, option => option.value))}>
              {[...available].map(([id, name]) => <option key={id} value={id}>{name}</option>)}
            </select>
          </label>
        </fieldset>
        <button type="submit" disabled={pending || !dirty}>Save schedule</button>
        {error && <p role="alert">{error}</p>}
      </form>
    </details>
  )
}

function ScheduleControls({ project, schedule, ownerChoices, onChanged }) {
  if (!ownerChoices) return <RecurringControls key={schedule.state} schedule={schedule} onChanged={onChanged} />
  return (
    <MaintenanceControls
      key={JSON.stringify([schedule.id, schedule.owner, schedule.cadence_hours, schedule.next_at,
        schedule.state, schedule.members])}
      project={project} schedule={schedule} owners={ownerChoices} onChanged={onChanged} />
  )
}

function ScheduleLink({ schedule, project, allProjects, preferTicket = false }) {
  const ticket = preferTicket && schedule.reusable_ticket
  if (ticket) return <TaskLink id={ticket.id} reference={ticket.key} title={schedule.title} />
  return <BoardLink href={scheduleHref(schedule.id, project, allProjects)}>{schedule.id} · {schedule.title}</BoardLink>
}

function ScheduleOwnerGroups({ items, renderRow }) {
  const owners = new Map()
  for (const item of items) {
    if (!owners.has(item.owner)) owners.set(item.owner, { name: item.owner_name || 'Unavailable assignee', items: [] })
    owners.get(item.owner).items.push(item)
  }
  const groups = [...owners].sort(([, left], [, right]) => left.name.localeCompare(right.name))
  return groups.map(([owner, group]) => (
    <section key={owner} className="schedule-owner" aria-label={group.name}>
      <h3>{group.name}</h3>
      {group.items.map(renderRow)}
    </section>
  ))
}

export function GoalReviewCadence({ schedule, reviewTime }) {
  const time = schedule?.review_time ?? reviewTime
  const interval = schedule?.cadence === 'interval'
  if (!interval && !time) return 'No schedule set'
  return <>
    {interval ? longInterval(schedule.cadence_hours) : <><time dateTime={time}>{time}</time> Pacific daily</>}
    {schedule?.state === 'paused' && ' · Paused'}
  </>
}

function longInterval(hours) {
  return hours === 1 ? 'Every hour' : `Every ${hours} hours`
}

export function GoalReviewTime({ goal, onChanged }) {
  const schedule = goal.review_schedule
  const savedMode = schedule?.cadence === 'interval' ? 'interval' : 'daily'
  const savedTime = schedule?.review_time ?? goal.review_time ?? ''
  const savedHours = String(schedule?.cadence_hours ?? '')
  const savedAnchor = asDeviceDate(schedule?.anchor_at, 19)
  const paused = schedule?.state === 'paused'
  const [draft, setDraft] = useState({})
  const { mode = savedMode, time = savedTime, hours = savedHours, anchor = savedAnchor } = draft
  const dirty = mode !== savedMode || (mode === 'daily' ? time !== savedTime : hours !== savedHours || anchor !== savedAnchor)
  const ready = mode === 'daily' ? Boolean(time) : Number.isSafeInteger(Number(hours)) && Number(hours) > 0 && Boolean(anchor)
  const { pending, error, save } = useScheduleSave(
    `/tasks/${encodeURIComponent(goal.ticket)}/recurrence`, () => { setDraft({}); onChanged() },
  )
  return (
    <details className="schedule-editor">
      <summary>Edit Review schedule</summary>
      <form className="schedule-review-form" onSubmit={event => {
        event.preventDefault()
        if (!ready || !dirty) return
        const settings = mode === 'daily' ? { review_time: time }
          : { cadence_hours: Number(hours), next_at: new Date(anchor).toISOString() }
        save({ change: 'enable', ...settings })
      }} data-reload-blocked={pending || dirty}>
        <label>Cadence
          <select value={mode} disabled={pending} onChange={event => {
            const value = event.currentTarget.value
            setDraft(current => ({ ...current, mode: value }))
          }}>
            <option value="daily">Daily at a Pacific time</option>
            <option value="interval">Fixed interval</option>
          </select>
        </label>
        {mode === 'daily' ? <label>Daily Review time (Pacific)
          <input type="time" value={time} required disabled={pending} onChange={event => {
            const value = event.currentTarget.value
            setDraft(current => ({ ...current, time: value }))
          }} />
        </label> : <>
          <label>Interval in hours
            <input type="number" min="1" step="1" value={hours} required disabled={pending}
              onChange={event => {
            const value = event.currentTarget.value
            setDraft(current => ({ ...current, hours: value }))
          }} />
          </label>
          <label>First Review (device time)
            <input type="datetime-local" step="1" value={anchor} required disabled={pending}
              onChange={event => {
            const value = event.currentTarget.value
            setDraft(current => ({ ...current, anchor: value }))
          }} />
          </label>
        </>}
        <div className="schedule-actions">
          <button type="submit" disabled={pending || !ready || !dirty}>Save Review schedule</button>
          {(schedule || savedTime) && <>
            <button type="button" disabled={pending}
              onClick={() => save({ change: paused ? 'resume' : 'pause' })}>{paused ? 'Resume' : 'Pause'}</button>
            <button type="button" disabled={pending} onClick={() => save({ change: 'cancel' })}>Cancel schedule</button>
          </>}
        </div>
        {error && <p role="alert">{error}</p>}
      </form>
    </details>
  )
}

function GoalReviews({ goals, allProjects, onChanged }) {
  return (
    <section className="panel schedule-panel" aria-labelledby="goal-reviews-title">
      <h2 id="goal-reviews-title">Goal reviews</h2>
      {goals.length === 0 && <p>{allProjects
        ? 'No active goals in your accessible projects.' : 'No active goals in this project.'}</p>}
      <ScheduleOwnerGroups items={goals} renderRow={goal => (
        <article key={goal.ticket} className="schedule-item">
          <h4><BoardLink href={taskHref(goal.ticket)}>{goal.ticket} · <MarkdownInline text={goal.title} links={false} /></BoardLink></h4>
          <dl className="schedule-summary">
            <div><dt>Project</dt><dd>{goal.project}</dd></div>
            <div><dt>Review schedule</dt><dd><GoalReviewCadence schedule={goal.review_schedule}
              reviewTime={goal.review_time} /></dd></div>
            {goal.review_schedule?.next_at && <div><dt>Next Review</dt><dd><ScheduleDate value={goal.review_schedule.next_at} /></dd></div>}
            {goal.deadline && <div><dt>Deadline</dt><dd><time dateTime={goal.deadline}>{goal.deadline}</time></dd></div>}
          </dl>
          {goal.editable === true && <GoalReviewTime goal={goal} onChanged={onChanged} />}
        </article>
      )} />
    </section>
  )
}

function CompleteScheduleReview({ schedule, onChanged }) {
  const [note, setNote] = useState('')
  const { pending, error, save } = useScheduleSave(
    `/schedules/${encodeURIComponent(schedule.id)}/complete-review`, () => { setNote(''); onChanged() },
  )
  return (
    <details className="schedule-editor">
      <summary>Complete review</summary>
      <form className="schedule-review-form" onSubmit={event => {
        event.preventDefault()
        save({ note: note.trim() })
      }} data-reload-blocked={pending || Boolean(note)}>
        <label>Review outcome
          <textarea rows="3" required value={note} disabled={pending} onChange={event => setNote(event.target.value)} />
        </label>
        <button type="submit" disabled={pending || !note.trim()}>Record outcome and park ticket</button>
        {error && <p role="alert">{error}</p>}
      </form>
    </details>
  )
}

export function ScheduleContents({ data, project, allProjects, onChanged }) {
  const { recurring, goal_reviews: goalReviews = [], maintenance, project_maintenance: projectMaintenance } = data
  const schedule = maintenance.schedule
  const [showCanceled, setShowCanceled] = useState(false)
  const hasCanceled = recurring.some(item => item.state === 'canceled')
  const visibleRecurring = showCanceled ? recurring : recurring.filter(item => item.state !== 'canceled')
  return (
    <>
      <section className="panel schedule-panel" aria-labelledby="recurring-title">
        <div className="schedule-section-heading">
          <h2 id="recurring-title">Recurring work</h2>
          {hasCanceled && <label>
            <input type="checkbox" checked={showCanceled} onChange={event => setShowCanceled(event.target.checked)} />
            Show canceled
          </label>}
        </div>
        {recurring.length === 0 && <p>{allProjects
          ? 'No recurring schedules in your accessible projects.' : 'No recurring schedules in this project.'}</p>}
        {hasCanceled && visibleRecurring.length === 0 && <p>All recurring schedules are canceled. Select Show canceled to see them.</p>}
        <ScheduleOwnerGroups items={visibleRecurring} renderRow={item => (
          <article key={item.id} className="schedule-item">
            <h4><ScheduleLink schedule={item} project={item.project || project} allProjects={allProjects} preferTicket /></h4>
            <ScheduleSummary schedule={item} project={item.project || project} showOwner={false} showTicket={false} />
            {!item.reusable_ticket && <>
              <OccurrenceHistory occurrences={item.recent_occurrences} />
              {!allProjects && item.editable === true && <ScheduleControls schedule={item} onChanged={onChanged} />}
            </>}
          </article>
        )} />
      </section>
      <GoalReviews goals={goalReviews} allProjects={allProjects} onChanged={onChanged} />
      <section className="panel schedule-panel" aria-labelledby="maintenance-title">
        <h2 id="maintenance-title">Project maintenance</h2>
        {allProjects && !projectMaintenance?.length && <p>You have no project maintenance schedules.</p>}
        {allProjects && projectMaintenance?.map(item => (
          <article key={item.id} className="schedule-item">
            <h3><ScheduleLink schedule={item} project={item.project} /></h3>
            <p>{item.project}</p>
            <ScheduleSummary schedule={item} />
            <OccurrenceHistory occurrences={item.recent_occurrences} />
          </article>
        ))}
        {!allProjects && !maintenance.editable && <p>Only the project manager can view and edit this schedule.</p>}
        {maintenance.editable && !schedule && <p>This project has no maintenance schedule.</p>}
        {schedule && (
          <>
            <h3><ScheduleLink schedule={schedule} project={project} /></h3>
            <ScheduleSummary schedule={schedule} />
            <OccurrenceHistory occurrences={schedule.recent_occurrences} />
            {maintenance.editable && (
              <ScheduleControls project={project} schedule={schedule}
                ownerChoices={maintenance.owner_choices} onChanged={onChanged} />
            )}
          </>
        )}
      </section>
    </>
  )
}

function ScheduleDetail({ data, onChanged }) {
  const { kind, project, runs, schedule, editable, owner_choices: ownerChoices } = data
  return (
    <>
      <section className="panel schedule-panel" aria-label="Schedule details">
        <ScheduleSummary schedule={schedule} project={project} />
        {schedule.can_complete_review === true && <CompleteScheduleReview schedule={schedule} onChanged={onChanged} />}
        {editable === true && <ScheduleControls project={project} schedule={schedule}
          ownerChoices={kind === 'maintenance' ? ownerChoices : undefined} onChanged={onChanged} />}
      </section>
      <section className="panel schedule-panel" aria-labelledby="schedule-runs">
        <h2 id="schedule-runs">Recent runs</h2>
        <RecurringRunList runs={runs} showOutcome />
      </section>
    </>
  )
}

export default function Schedules({
  id, project, requestedProject, projectPicker, session, verified, revision, onChanged, allProjects,
}) {
  const login = useSignInHref(id ? scheduleHref(id, requestedProject, allProjects) : schedulesHref(project, allProjects))
  const path = id ? queryPath(`/schedules/${id}`, { project: requestedProject })
    : queryPath('/schedules', { view: 'team', ...(allProjects ? { scope: 'all' } : { project }) })
  const resource = useResource(verified ? path : null, revision)
  const listProject = requestedProject || resource.data?.project || project
  const detailTitle = resource.data?.schedule?.title
  const heading = id && detailTitle ? `${id} · ${detailTitle}` : id || 'Schedules'
  useContentTitle(id && detailTitle ? heading : null)
  const label = id ? 'schedule' : 'schedules'
  return (
    <>
      <div className="page-heading schedule-heading">
        <h1 id={id ? `schedule-${id}` : 'schedules'}>{heading}</h1>
        {id ? <BoardLink href={schedulesHref(listProject, allProjects)}>Back to Schedules</BoardLink> : <>
          {projectPicker}
          <BoardLink href={'#' + new URLSearchParams(allProjects ? { scope: 'all' } : { project })}>Back to Tasks</BoardLink>
        </>}
      </div>
      {verified ? (
        <>
          {id && resource.error?.status === 404 ? <p className="section-message" role="status">Schedule unavailable.</p>
            : <ResourceStatus resource={resource} label={label} />}
          {resource.data && (id ? <ScheduleDetail data={resource.data} onChanged={onChanged} />
            : <ScheduleContents data={resource.data} project={project} allProjects={allProjects} onChanged={onChanged} />)}
        </>
      ) : !session.data && (session.pending || (session.error && session.error.status !== 401)) ? (
        <ResourceStatus resource={session} label={label} />
      ) : (
        <p>{session.data?.sign_in_available || session.error?.status === 401
          ? <a href={login}>Sign in with Raft</a>
          : 'Schedules are available on the signed-in board.'}</p>
      )}
    </>
  )
}
