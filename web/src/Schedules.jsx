import React, { useState } from 'react'
import { queryPath, request, useResource } from './data.js'
import { ResourceStatus, scheduleHref, schedulesHref, signInHref, taskHref, taskLabel } from './shared.jsx'
import { useTimeZone } from './timeZone.js'
import { RecurringRunList } from './RecurringRuns.jsx'

const scheduleStates = { active: 'Active', paused: 'Paused', canceled: 'Canceled' }
const runStates = {
  pending: 'Pending',
  missed: 'Missed',
  completed_on_time: 'Completed on time',
  completed_late: 'Completed late',
}

function asDeviceDate(value) {
  if (!value) return ''
  const date = new Date(value)
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
}

function ScheduleDate({ value }) {
  const { formatTimestamp } = useTimeZone()
  return value ? <time dateTime={value}>{formatTimestamp(value)}</time> : '—'
}

export function ScheduleSummary({ schedule }) {
  const { owner_name: owner, cadence_hours: cadence, state, next_at: next } = schedule
  const interval = cadence % 24 === 0
    ? cadence === 24 ? 'Daily' : `Every ${cadence / 24} days`
    : cadence === 1 ? 'Hourly' : `Every ${cadence} hours`
  return (
    <dl className="schedule-summary">
      <div><dt>Owner</dt><dd>{owner || 'Unavailable assignee'}</dd></div>
      <div><dt>Cadence</dt><dd>{interval}</dd></div>
      <div><dt>Status</dt><dd>{scheduleStates[state]}</dd></div>
      {state === 'active' && <div><dt>Next run</dt><dd><ScheduleDate value={next} /></dd></div>}
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
              <a href={taskHref(run.task_id, run.task_key)}>{taskLabel(run.task_id, run.task_key)}</a>
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

function ScheduleLink({ schedule, project }) {
  return <a href={scheduleHref(schedule.id, project)}>{schedule.id} · {schedule.title}</a>
}

export function ScheduleContents({ data, project, allProjects, onChanged }) {
  const { recurring, maintenance, project_maintenance: projectMaintenance } = data
  const schedule = maintenance.schedule
  return (
    <>
      <section className="panel schedule-panel" aria-labelledby="recurring-title">
        <h2 id="recurring-title">Your recurring work</h2>
        {recurring.length === 0 && <p>{allProjects
          ? 'You have no recurring schedules.' : 'You have no recurring schedules in this project.'}</p>}
        {recurring.map(item => (
          <article key={item.id} className="schedule-item">
            <h3><ScheduleLink schedule={item} project={item.project || project} /></h3>
            {allProjects && <p>{item.project}</p>}
            <ScheduleSummary schedule={item} />
            <OccurrenceHistory occurrences={item.recent_occurrences} />
            {!allProjects && <ScheduleControls schedule={item} onChanged={onChanged} />}
          </article>
        ))}
      </section>
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
  const { kind, project, runs, schedule, owner_choices: ownerChoices } = data
  return (
    <>
      <section className="panel schedule-panel" aria-label="Schedule details">
        <ScheduleSummary schedule={schedule} />
        <ScheduleControls project={project} schedule={schedule}
          ownerChoices={kind === 'maintenance' ? ownerChoices : undefined} onChanged={onChanged} />
      </section>
      <section className="panel schedule-panel" aria-labelledby="schedule-runs">
        <h2 id="schedule-runs">Recent runs</h2>
        <RecurringRunList runs={runs} />
      </section>
    </>
  )
}

export default function Schedules({ id, project, requestedProject, projectPicker, session, verified, revision, onChanged, allProjects }) {
  const path = id ? queryPath(`/schedules/${id}`, { project: requestedProject })
    : queryPath('/schedules', allProjects ? { scope: 'all' } : { project })
  const resource = useResource(verified ? path : null, revision)
  const listProject = resource.data?.project || project
  const detailTitle = resource.data?.schedule?.title
  const heading = id && detailTitle ? `${id} · ${detailTitle}` : id || 'Schedules'
  const label = id ? 'schedule' : 'schedules'
  return (
    <>
      <div className="page-heading schedule-heading">
        <h1 id={id ? `schedule-${id}` : 'schedules'}>{heading}</h1>
        {id ? <a href={schedulesHref(listProject)}>Back to Schedules</a> : <>
          {projectPicker}
          <a href={'#' + new URLSearchParams(allProjects ? { scope: 'all' } : { project })}>Back to Tasks</a>
        </>}
      </div>
      {verified ? (
        <>
          {id && resource.error?.status === 404 ? <p className="section-message" role="status">Schedule unavailable.</p>
            : <ResourceStatus resource={resource} label={label} />}
          {resource.data && (id ? <ScheduleDetail data={resource.data} onChanged={onChanged} />
            : <ScheduleContents data={resource.data} project={project} allProjects={allProjects} onChanged={onChanged} />)}
        </>
      ) : (
        <p>{session.data?.sign_in_available || session.error?.status === 401
          ? <a href={signInHref(id ? scheduleHref(id, requestedProject) : schedulesHref(project, allProjects))}>Sign in with Raft</a>
          : 'Schedules are available on the signed-in board.'}</p>
      )}
    </>
  )
}
