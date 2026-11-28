import React, { useState } from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { Markdown } from './Markdown.jsx'
import { ReportTime } from './ReportTime.jsx'
import { eventHref, taskHref } from './shared.jsx'
import TransitionFeedback, { useTransitionRequest } from './TransitionFeedback.jsx'

function occurrenceLabel(kind) {
  return kind === 'review' ? 'Scheduled Review' : 'Explore'
}

function GoalOccurrenceForm({ occurrence, onCompleted }) {
  const [note, setNote] = useState('')
  const [url, setUrl] = useState('')
  const label = occurrenceLabel(occurrence.kind)
  const mutation = useTransitionRequest(receipt => onCompleted(receipt, occurrence))

  function submit(event) {
    event.preventDefault()
    if (!note.trim()) return
    mutation.submit(`/tasks/${encodeURIComponent(occurrence.ticket)}/goal-occurrences/complete`, {
      occurrence_key: occurrence.key, note: note.trim(), ...(url.trim() && { url: url.trim() }),
    })
  }

  return (
    <details>
      <summary>{label}</summary>
      {occurrence.due_at && <p className="event-meta">Due <ReportTime at={occurrence.due_at} /></p>}
      {occurrence.goal_review_prompt?.prompt && <Markdown text={occurrence.goal_review_prompt.prompt} />}
      <form className="profile-form" onSubmit={submit} data-reload-blocked={Boolean(note || url || mutation.saving)}>
        <label>Outcome and next action
          <textarea rows={3} value={note} onChange={event => setNote(event.target.value)}
            disabled={mutation.saving} required />
        </label>
        <label>Evidence link (optional)
          <input type="url" value={url} onChange={event => setUrl(event.target.value)} disabled={mutation.saving} />
        </label>
        <button type="submit" disabled={mutation.blocked || !note.trim()}>
          {mutation.saving ? 'Recording…' : `Record ${label}`}
        </button>
        <TransitionFeedback mutation={mutation} />
      </form>
    </details>
  )
}

export default function GoalOccurrences({ items = [], onChanged, showTicket = false }) {
  const [completed, setCompleted] = useState({})
  const available = items.filter(occurrence => !Object.hasOwn(completed, occurrence.key))
  const receipts = Object.values(completed)
  if (!available.length && !receipts.length) return null

  function record(receipt, occurrence) {
    setCompleted(previous => ({ ...previous, [occurrence.key]: { ...occurrence, eventId: receipt.event_id } }))
    onChanged()
  }

  return (
    <section aria-label="Goal work">
      <h3>Goal work</h3>
      <ul className="event-list">
        {available.map(item => (
          <li className="event-row" key={item.key}>
            {showTicket && <BoardLink href={taskHref(item.ticket)}>{item.ticket}</BoardLink>}
            <GoalOccurrenceForm occurrence={item} onCompleted={record} />
          </li>
        ))}
        {receipts.map(item => (
          <li className="event-row" key={item.key} role="status">
            {showTicket && <BoardLink href={taskHref(item.ticket)}>{item.ticket}</BoardLink>}
            <BoardLink href={eventHref(item.eventId)}>{occurrenceLabel(item.kind)} recorded</BoardLink>
          </li>
        ))}
      </ul>
    </section>
  )
}
