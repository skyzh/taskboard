import React, { useState } from 'react'
import { queryPath, request, useResource } from './data.js'
import { ResourceStatus, signInHref, taskHref, taskLabel } from './shared.jsx'
import { useTimeZone } from './timeZone.js'

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
      <div><dt>Schedule</dt><dd>{interval}</dd></div>
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

export function ScheduleContents({ data, project, onChanged }) {
  const { recurring, maintenance } = data
  const schedule = maintenance.schedule
  return (
    <>
      {project === 'T' && (
        <section className="panel schedule-panel" aria-labelledby="recurring-title">
          <h2 id="recurring-title">Your recurring work</h2>
          {recurring.length === 0 && <p>You have no recurring schedules.</p>}
          {recurring.map(item => (
            <article key={item.id} className="schedule-item">
              <h3>{item.title}</h3>
              <ScheduleSummary schedule={item} />
              <OccurrenceHistory occurrences={item.recent_occurrences} />
              <RecurringControls key={item.state} schedule={item} onChanged={onChanged} />
            </article>
          ))}
        </section>
      )}
      <section className="panel schedule-panel" aria-labelledby="maintenance-title">
        <h2 id="maintenance-title">Project maintenance</h2>
        {!maintenance.editable && <p>Only the project manager can view and edit this schedule.</p>}
        {maintenance.editable && !schedule && <p>This project has no maintenance schedule.</p>}
        {schedule && (
          <>
            <ScheduleSummary schedule={schedule} />
            <OccurrenceHistory occurrences={schedule.recent_occurrences} />
            {maintenance.editable && (
              <MaintenanceControls
                key={JSON.stringify([schedule.id, schedule.owner, schedule.cadence_hours, schedule.next_at,
                  schedule.state, schedule.members])}
                project={project} schedule={schedule} owners={maintenance.owner_choices} onChanged={onChanged} />
            )}
          </>
        )}
      </section>
    </>
  )
}

export default function Schedules({ project, session, verified, revision, onChanged }) {
  const resource = useResource(verified ? queryPath('/schedules', { project }) : null, revision)
  const projectHash = new URLSearchParams({ project })
  return (
    <>
      <div className="page-heading">
        <h1 id="schedules">Schedules</h1>
        <a href={'#' + projectHash}>Back to Tasks</a>
      </div>
      {verified ? (
        <>
          <ResourceStatus resource={resource} label="schedules" />
          {resource.data && <ScheduleContents data={resource.data} project={project} onChanged={onChanged} />}
        </>
      ) : (
        <p>{session.data?.sign_in_available || session.error?.status === 401
          ? <a href={signInHref('#' + new URLSearchParams({ schedules: '', project }))}>Sign in with Raft</a>
          : 'Schedules are available on the signed-in board.'}</p>
      )}
    </>
  )
}
