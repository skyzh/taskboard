import React, { useState } from 'react'
import { request } from './data.js'
import { ReminderValue } from './ReminderDate.jsx'
import { TaskLink } from './shared.jsx'
import { useTimeZone } from './timeZone.js'

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
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const timed = mode === 'time'
  const reviewing = mode === 'dependency'
  const dated = mode === 'date' || timed
  const dirty = mode !== savedMode || note !== savedNote || date !== savedDate || time !== savedTime
    || dependency !== (savedDependency?.source_key || '') || criteria !== (savedDependency?.criteria || '')
  const hasInput = (mode === 'keep' && note.trim() !== savedNote.trim()) || (timed && time)
    || (mode === 'date' && date) || (reviewing && dependency.trim() && criteria.trim())
  const canSave = Boolean(note.trim() && hasInput && !pending)
  const path = `/tasks/${encodeURIComponent(task.key || task.id)}`

  async function save(suffix, body, method) {
    setPending(true)
    setError('')
    try {
      await request(path + suffix, { body, method })
      onChanged()
    } catch (failure) {
      setError(failure.message)
    } finally {
      setPending(false)
    }
  }

  function submit(event) {
    event.preventDefault()
    if (!canSave) return
    const body = { note: note.trim() }
    if (timed) Object.assign(body, { at: time, time_zone: timeZone })
    if (mode === 'date') body.review_after = date
    if (reviewing) Object.assign(body, { review_on: dependency.trim(), review_criteria: criteria.trim() })
    save('/parking', body, 'POST')
  }

  return (
    <details className="task-ownership">
      <summary>{parked ? (savedReturnPath ? 'Edit parking' : 'Add a return path') : 'Park work'}</summary>
      <form className="ownership-form" onSubmit={submit} data-reload-blocked={pending || dirty}>
        <p>Choose when to revisit this work. If someone or another ticket must act first, use Waiting on.</p>
        {savedDependency && <p>Review after <TaskLink id={savedDependency.source_id} reference={savedDependency.source_key} /> changes.</p>}
        <label>Return to Inbox
          <select value={mode} onChange={event => setMode(event.target.value)} disabled={pending}>
            {savedReturnPath && <option value="keep">Keep current return path</option>}
            <option value="date">Review date</option>
            <option value="time">Exact time</option>
            <option value="dependency">Dependency review</option>
          </select>
        </label>
        {dated && <ReminderValue timed={timed} value={timed ? time : date} onChange={timed ? setTime : setDate}
          timeZone={timeZone} disabled={pending} />}
        {reviewing && <>
          <label>Dependency ticket<input value={dependency} onChange={event => setDependency(event.target.value)}
            placeholder="T-123" maxLength={28} required disabled={pending} /></label>
          <label>What should you check when it changes?
            <textarea value={criteria} onChange={event => setCriteria(event.target.value)}
              rows={2} maxLength={4000} required disabled={pending} />
          </label>
        </>}
        <label>Reason for parking
          <textarea value={note} onChange={event => setNote(event.target.value)}
            rows={2} maxLength={4000} required disabled={pending} />
        </label>
        {error && <p role="alert">{error}</p>}
        <button type="submit" disabled={!canSave}>{pending ? 'Saving…' : parked ? 'Save parking' : 'Park task'}</button>
        {savedReminder && savedDependency && <button type="button" disabled={pending}
          onClick={() => save('/reminder', { date: null }, 'PUT')}>Clear reminder</button>}
      </form>
    </details>
  )
}
