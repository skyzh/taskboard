import React, { useState } from 'react'
import { FieldHelp } from './FieldGuidance.jsx'
import TransitionFeedback, { useTransitionRequest } from './TransitionFeedback.jsx'

export default function TaskDetailsEdit({ task, brief, onChanged, fields }) {
  const titleHelpId = `ticket-title-guidance-${task.id}`
  const briefHelpId = `ticket-brief-guidance-${task.id}`
  const [draft, setDraft] = useState({})
  const [reason, setReason] = useState('')
  const mutation = useTransitionRequest(() => {
    setDraft({})
    setReason('')
    onChanged()
  })
  const title = draft.title ?? task.title
  const instructions = draft.brief ?? brief ?? ''
  const changed = {
    ...(draft.title != null && title.trim() !== task.title && { title: title.trim() }),
    ...(draft.brief != null && instructions.trim() !== (brief ?? '') && { brief: instructions.trim() }),
  }
  const canSave = Object.keys(changed).length > 0 && title.trim() && reason.trim() && !mutation.blocked

  function submit(event) {
    event.preventDefault()
    if (!canSave) return
    return mutation.submit(`/tasks/${encodeURIComponent(task.key || task.id)}/details`,
      { ...changed, reason: reason.trim() })
  }

  return (
    <details className="task-ownership">
      <summary>Edit title and brief</summary>
      <form className="ownership-form" onSubmit={submit}
        data-reload-blocked={Boolean(mutation.blocked || Object.keys(draft).length || reason)}>
        <label>
          Title
          <input value={title} onChange={event => setDraft({ ...draft, title: event.target.value })}
            aria-describedby={fields?.title ? titleHelpId : undefined}
            disabled={mutation.blocked} required />
        </label>
        <FieldHelp field={fields?.title} id={titleHelpId} />
        <label>
          Brief
          <textarea value={instructions} onChange={event => setDraft({ ...draft, brief: event.target.value })}
            aria-describedby={fields?.brief ? briefHelpId : undefined}
            rows={4} disabled={mutation.blocked} />
        </label>
        <FieldHelp field={fields?.brief} id={briefHelpId} />
        <label>
          Reason for change
          <textarea value={reason} onChange={event => setReason(event.target.value)}
            rows={2} disabled={mutation.blocked} required />
        </label>
        <button type="submit" disabled={!canSave}>{mutation.saving ? 'Saving…' : 'Save details'}</button>
        <TransitionFeedback mutation={mutation} />
      </form>
    </details>
  )
}
