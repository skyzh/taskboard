import React, { useId, useState } from 'react'
import { FieldHelp } from './FieldGuidance.jsx'
import TransitionFeedback, { recommendedTextLength, useTransitionRequest } from './TransitionFeedback.jsx'

export default function TaskActivityUpdate({ task, onChanged, field, confirmBlocker = false, decision = false }) {
  const activityHelpId = useId()
  const lengthHelpId = `${activityHelpId}-length`
  const [note, setNote] = useState('')
  const [details, setDetails] = useState('')
  const [evidence, setEvidence] = useState('')
  const mutation = useTransitionRequest(() => {
    setNote('')
    setDetails('')
    setEvidence('')
    onChanged()
  }, confirmBlocker ? 'Blocker confirmed.' : decision ? 'Decision recorded.' : 'Update added.')
  const evidenceRequired = confirmBlocker && !task.context?.blocked_by
  const canSubmit = note.trim() && (!decision || details.trim()) && (!evidenceRequired || evidence.trim()) && !mutation.blocked
  const longUpdate = !confirmBlocker && [...note.trim()].length > recommendedTextLength
  const description = [field && activityHelpId, longUpdate && lengthHelpId].filter(Boolean).join(' ')

  function submit(event) {
    event.preventDefault()
    if (!canSubmit) return
    const action = confirmBlocker ? 'confirm-blocker' : decision ? 'activity/decision' : 'activity'
    return mutation.submit(`/tasks/${encodeURIComponent(task.key || task.id)}/${action}`, {
      note: note.trim(),
      ...(decision && { details: details.trim() }),
      ...(confirmBlocker && { wait_event: task.event_id }),
      ...(evidence.trim() && { url: evidence.trim() }),
    })
  }

  return (
    <details className="task-ownership">
      <summary>{confirmBlocker ? 'Confirm blocker cleared' : decision ? 'Record Decision' : 'Add update'}</summary>
      <form className="ownership-form" onSubmit={submit} data-reload-blocked={Boolean(mutation.blocked || note || details || evidence)}>
        <label>
          {confirmBlocker ? 'Verified outcome' : decision ? 'Choice' : 'Update'}
          <textarea value={note} onChange={event => { setNote(event.target.value); mutation.clear() }}
            aria-describedby={description || undefined} rows={3} required disabled={mutation.blocked} />
        </label>
        {decision && <label>
          Rationale and what changes next
          <textarea value={details} onChange={event => { setDetails(event.target.value); mutation.clear() }}
            rows={3} required disabled={mutation.blocked} />
        </label>}
        <FieldHelp field={field} id={activityHelpId} />
        {longUpdate && <p className="field-guidance-help" id={lengthHelpId}>
          This update exceeds the recommended length of {recommendedTextLength.toLocaleString()} characters.
          You can still post the full text.
        </p>}
        <label>
          {evidenceRequired ? 'Evidence link' : 'Evidence link (optional)'}
          <input type="url" value={evidence} onChange={event => { setEvidence(event.target.value); mutation.clear() }}
            maxLength={2000} required={evidenceRequired} disabled={mutation.blocked} />
        </label>
        <button type="submit" disabled={!canSubmit}>
          {mutation.saving ? 'Posting…' : confirmBlocker ? 'Confirm and return to Ready' : decision ? 'Post Decision' : 'Post update'}
        </button>
        <TransitionFeedback mutation={mutation} />
      </form>
    </details>
  )
}
