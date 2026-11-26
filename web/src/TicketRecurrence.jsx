import React, { useState } from 'react'
import { ScheduleSummary } from './Schedules.jsx'
import TransitionFeedback, { useTransitionRequest } from './TransitionFeedback.jsx'

function RecurrenceForm({ change, cadence, disabled, onSubmit }) {
  const initialHours = cadence == null ? '' : String(cadence)
  const [hours, setHours] = useState(initialHours)
  const [next, setNext] = useState('')
  const [note, setNote] = useState('')
  const completing = change === 'complete'
  const enabling = change === 'enable'
  const action = completing ? 'Complete review' : enabling ? 'Enable recurrence' : 'Resume recurrence'
  const dirty = hours !== initialHours || Boolean(next || note)

  function submit(event) {
    event.preventDefault()
    if (completing) {
      if (note.trim()) onSubmit({ change, note: note.trim() })
      return
    }
    const timestamp = new Date(next)
    if (!Number.isFinite(timestamp.getTime())) return
    onSubmit({ change, next_at: timestamp.toISOString(),
      ...((enabling || hours !== initialHours) && { cadence_hours: Number(hours) }) })
  }

  return (
    <details className="schedule-editor">
      <summary>{action}</summary>
      <form className="schedule-review-form" onSubmit={submit} data-reload-blocked={disabled || dirty}>
        {enabling && <p>The ticket will park until its first scheduled run.</p>}
        {!completing && <label>Cadence in hours
          <input type="number" min="1" step="1" required value={hours} disabled={disabled}
            onChange={event => setHours(event.target.value)} />
        </label>}
        {completing ? (
          <label>Review outcome
            <textarea rows={3} required value={note} disabled={disabled} onChange={event => setNote(event.target.value)} />
          </label>
        ) : (
          <label>{enabling ? 'First run (device time)' : 'Next run (device time)'}
            <input type="datetime-local" required value={next} disabled={disabled} onChange={event => setNext(event.target.value)} />
          </label>
        )}
        <button type="submit" disabled={disabled || (completing && !note.trim())}>
          {completing ? 'Record outcome and park ticket' : enabling ? 'Enable' : 'Resume'}
        </button>
      </form>
    </details>
  )
}

export default function TicketRecurrence({ ticket, schedule, canEnable, signedIn, onChanged }) {
  const [confirmCancel, setConfirmCancel] = useState(false)
  const mutation = useTransitionRequest(() => {
    setConfirmCancel(false)
    onChanged()
  }, 'Recurrence updated.')
  if (!schedule && !canEnable) return null

  const edit = signedIn && schedule?.editable === true
  const complete = signedIn && schedule?.can_complete_review === true
  const submit = body => mutation.submit(`/tasks/${encodeURIComponent(ticket)}/recurrence`, body)
  return (
    <section className="ticket-recurrence" aria-labelledby="ticket-recurrence-title">
      <h3 id="ticket-recurrence-title">Recurrence</h3>
      {schedule && <ScheduleSummary schedule={schedule} showOwner={false} showTicket={false} showNextRun={false} />}
      <div className="ticket-recurrence-actions">
        {signedIn && !schedule && canEnable && <RecurrenceForm
          change="enable" disabled={mutation.blocked} onSubmit={submit} />}
        {edit && schedule.state === 'active' && <button type="button" disabled={mutation.blocked}
          onClick={() => submit({ change: 'pause' })}>Pause recurrence</button>}
        {edit && schedule.state === 'paused' && <RecurrenceForm
          change="resume" cadence={schedule.cadence_hours} disabled={mutation.blocked} onSubmit={submit} />}
        {complete && <RecurrenceForm
          change="complete" disabled={mutation.blocked} onSubmit={submit} />}
        {edit && schedule.state !== 'canceled' && (
          <div className="schedule-actions" data-reload-blocked={confirmCancel || mutation.blocked}>
            {confirmCancel ? (
              <>
                <button type="button" disabled={mutation.blocked} onClick={() => submit({ change: 'cancel' })}>Confirm cancellation</button>
                <button type="button" disabled={mutation.blocked} onClick={() => setConfirmCancel(false)}>Keep recurrence</button>
              </>
            ) : <button type="button" disabled={mutation.blocked} onClick={() => setConfirmCancel(true)}>Cancel recurrence</button>}
          </div>
        )}
        <TransitionFeedback mutation={mutation} />
      </div>
    </section>
  )
}
