import React, { useState } from 'react'
import { ReminderValue } from './ReminderDate.jsx'
import { TaskLink } from './shared.jsx'
import { useTimeZone } from './timeZone.js'
import TransitionFeedback, { useTransitionRequest } from './TransitionFeedback.jsx'

export default function ParkingForm({ task, onChanged }) {
  const { timeZone, formatLocalInput } = useTimeZone()
  const context = task.context || {}
  const parked = task.state === 'parked'
  const savedReminder = parked && Boolean(context.review_after || context.defer_until)
  const savedDependency = parked && context.readiness?.source_key && context.readiness?.criteria ? context.readiness : null
  const savedReturnPath = savedReminder || savedDependency
  const savedDate = context.review_after || ''
  const savedTime = context.defer_until ? formatLocalInput(context.defer_until) : ''
  const savedNote = task.note || ''
  const savedMode = savedReturnPath ? 'keep' : 'date'
  const [mode, setMode] = useState(savedMode)
  const [date, setDate] = useState(savedDate)
  const [time, setTime] = useState(savedTime)
  const [dependency, setDependency] = useState(savedDependency?.source_key || '')
  const [criteria, setCriteria] = useState(savedDependency?.criteria || '')
  const [note, setNote] = useState(savedNote)
  const mutation = useTransitionRequest(onChanged)
  const timed = mode === 'time'
  const reviewing = mode === 'dependency'
  const dated = mode === 'date' || timed
  const dirty = mode !== savedMode || note !== savedNote || date !== savedDate || time !== savedTime
    || dependency !== (savedDependency?.source_key || '') || criteria !== (savedDependency?.criteria || '')
  const hasInput = (mode === 'keep' && note.trim() !== savedNote.trim()) || (timed && time)
    || (mode === 'date' && date) || (reviewing && dependency.trim() && criteria.trim())
  const canSave = Boolean(note.trim() && hasInput && !mutation.blocked)
  const path = `/tasks/${encodeURIComponent(task.key || task.id)}`

  function submit(event) {
    event.preventDefault()
    if (!canSave) return
    const body = { note: note.trim() }
    if (timed) Object.assign(body, { at: time, time_zone: timeZone })
    if (mode === 'date') body.review_after = date
    if (reviewing) Object.assign(body, { review_on: dependency.trim(), review_criteria: criteria.trim() })
    return mutation.submit(path + '/parking', body)
  }

  return (
    <details className="task-ownership">
      <summary>{parked ? (savedReturnPath ? 'Edit parking' : 'Add a return path') : 'Park work'}</summary>
      <form className="ownership-form" onSubmit={submit} data-reload-blocked={mutation.blocked || dirty}>
        <p>Choose when to revisit this work. If someone or another ticket must act first, use Waiting on.</p>
        {savedDependency && <p>Review after <TaskLink id={savedDependency.source_id} reference={savedDependency.source_key} /> changes.</p>}
        <label>Return to Inbox
          <select value={mode} onChange={event => setMode(event.target.value)} disabled={mutation.blocked}>
            {savedReturnPath && <option value="keep">Keep current return path</option>}
            <option value="date">Review date</option>
            <option value="time">Exact time</option>
            <option value="dependency">Dependency review</option>
          </select>
        </label>
        {dated && <ReminderValue timed={timed} value={timed ? time : date} onChange={timed ? setTime : setDate}
          timeZone={timeZone} disabled={mutation.blocked} />}
        {reviewing && <>
          <label>Dependency ticket<input value={dependency} onChange={event => setDependency(event.target.value)}
            placeholder="T-123" maxLength={28} required disabled={mutation.blocked} /></label>
          <label>What should you check when it changes?
            <textarea value={criteria} onChange={event => setCriteria(event.target.value)}
              rows={2} required disabled={mutation.blocked} />
          </label>
        </>}
        <label>Reason for parking
          <textarea value={note} onChange={event => setNote(event.target.value)}
            rows={2} required disabled={mutation.blocked} />
        </label>
        <button type="submit" disabled={!canSave}>{mutation.saving ? 'Saving…' : parked ? 'Save parking' : 'Park task'}</button>
        {savedReminder && savedDependency && <button type="button" disabled={mutation.blocked}
          onClick={() => mutation.submit(path + '/reminder', { date: null }, 'PUT')}>Clear reminder</button>}
        <TransitionFeedback mutation={mutation} />
      </form>
    </details>
  )
}
