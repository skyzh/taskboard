import React, { useId, useState } from 'react'
import { FieldHelp } from './FieldGuidance.jsx'
import TransitionFeedback, { recommendedTextLength, useTransitionRequest } from './TransitionFeedback.jsx'

export default function TaskActivityUpdate({ task, onChanged, field, confirmBlocker = false }) {
  const activityHelpId = useId()
  const lengthHelpId = `${activityHelpId}-length`
  const [note, setNote] = useState('')
  const [evidence, setEvidence] = useState('')
  const mutation = useTransitionRequest(() => {
    setNote('')
    setEvidence('')
    onChanged()
  }, confirmBlocker ? 'Blocker confirmed.' : 'Update added.')
  const evidenceRequired = confirmBlocker && !task.context?.blocked_by
  const canSubmit = note.trim() && (!evidenceRequired || evidence.trim()) && !mutation.blocked
  const longUpdate = !confirmBlocker && [...note.trim()].length > recommendedTextLength
  const description = [field && activityHelpId, longUpdate && lengthHelpId].filter(Boolean).join(' ')

  function submit(event) {
    event.preventDefault()
    if (!canSubmit) return
    const action = confirmBlocker ? 'confirm-blocker' : 'activity'
    return mutation.submit(`/tasks/${encodeURIComponent(task.key || task.id)}/${action}`, {
      note: note.trim(),
      ...(confirmBlocker && { wait_event: task.event_id }),
      ...(evidence.trim() && { url: evidence.trim() }),
    })
  }

  return (
    <details className="task-ownership">
      <summary>{confirmBlocker ? 'Confirm blocker cleared' : 'Add update'}</summary>
      <form className="ownership-form" onSubmit={submit} data-reload-blocked={Boolean(mutation.blocked || note || evidence)}>
        <label>
          {confirmBlocker ? 'Verified outcome' : 'Update'}
          <textarea value={note} onChange={event => { setNote(event.target.value); mutation.clear() }}
            aria-describedby={description || undefined} rows={3} required disabled={mutation.blocked} />
        </label>
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
          {mutation.saving ? 'Posting…' : confirmBlocker ? 'Confirm and return to Ready' : 'Post update'}
        </button>
        <TransitionFeedback mutation={mutation} />
      </form>
    </details>
  )
}
