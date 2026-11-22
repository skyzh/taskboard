import React, { useState } from 'react'
import { request } from './data.js'

const titleLimit = 60

export default function TaskDetailsEdit({ task, brief, onChanged }) {
  const [draft, setDraft] = useState({})
  const [reason, setReason] = useState('')
  const [saving, setSaving] = useState(false)
  const [failure, setFailure] = useState(null)
  const title = draft.title ?? task.title
  const instructions = draft.brief ?? brief ?? ''
  const changed = {
    ...(draft.title != null && title.trim() !== task.title && { title: title.trim() }),
    ...(draft.brief != null && instructions.trim() !== (brief ?? '') && { brief: instructions.trim() }),
  }
  const overLimit = changed.title != null && Array.from(changed.title).length > titleLimit
  const titleErrorId = `task-title-limit-${task.id}`
  const canSave = Object.keys(changed).length > 0 && title.trim() && reason.trim() && !overLimit && !saving

  async function submit(event) {
    event.preventDefault()
    if (!canSave) return
    setSaving(true)
    setFailure(null)
    try {
      await request(`/tasks/${encodeURIComponent(task.key || task.id)}/details`, {
        method: 'POST',
        body: { ...changed, reason: reason.trim() },
      })
      setDraft({})
      setReason('')
      onChanged()
    } catch (error) {
      setFailure(error.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <details className="task-ownership">
      <summary>Edit title and brief</summary>
      <form className="ownership-form" onSubmit={submit}
        data-reload-blocked={Boolean(saving || Object.keys(draft).length || reason)}>
        <label>
          Title
          <input value={title} onChange={event => setDraft({ ...draft, title: event.target.value })}
            aria-invalid={overLimit || undefined} aria-describedby={overLimit ? titleErrorId : undefined}
            disabled={saving} required />
        </label>
        {overLimit && <p id={titleErrorId} role="alert">Keep the title to {titleLimit} characters. Move details into the brief.</p>}
        <label>
          Brief
          <textarea value={instructions} onChange={event => setDraft({ ...draft, brief: event.target.value })}
            rows={4} maxLength={4000} disabled={saving} />
        </label>
        <label>
          Reason for change
          <textarea value={reason} onChange={event => setReason(event.target.value)}
            rows={2} maxLength={4000} disabled={saving} required />
        </label>
        {failure && <p role="alert">{failure}</p>}
        <button type="submit" disabled={!canSave}>{saving ? 'Saving…' : 'Save details'}</button>
      </form>
    </details>
  )
}
