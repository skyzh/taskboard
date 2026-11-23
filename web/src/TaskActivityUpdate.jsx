import React, { useId, useState } from 'react'
import { request } from './data.js'
import { FieldHelp } from './FieldGuidance.jsx'

export default function TaskActivityUpdate({ task, onChanged, field, confirmBlocker = false }) {
  const activityHelpId = useId()
  const [note, setNote] = useState('')
  const [evidence, setEvidence] = useState('')
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState(null)
  const evidenceRequired = confirmBlocker && !task.context?.blocked_by
  const canSubmit = note.trim() && (!evidenceRequired || evidence.trim()) && !saving

  async function submit(event) {
    event.preventDefault()
    if (!canSubmit) return
    setSaving(true)
    setResult(null)
    try {
      const action = confirmBlocker ? 'confirm-blocker' : 'activity'
      await request(`/tasks/${encodeURIComponent(task.key || task.id)}/${action}`, {
        body: {
          note: note.trim(),
          ...(confirmBlocker && { wait_event: task.event_id }),
          ...(evidence.trim() && { url: evidence.trim() }),
        },
      })
      setNote('')
      setEvidence('')
      onChanged()
      setResult({ message: confirmBlocker ? 'Blocker confirmed.' : 'Update added.' })
    } catch (error) {
      setResult({ error: true, message: error.message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <details className="task-ownership">
      <summary>{confirmBlocker ? 'Confirm blocker cleared' : 'Add update'}</summary>
      <form className="ownership-form" onSubmit={submit} data-reload-blocked={Boolean(saving || note || evidence)}>
        <label>
          {confirmBlocker ? 'Verified outcome' : 'Update'}
          <textarea value={note} onChange={event => { setNote(event.target.value); setResult(null) }}
            aria-describedby={field ? activityHelpId : undefined} rows={3} maxLength={4000} required disabled={saving} />
        </label>
        <FieldHelp field={field} id={activityHelpId} />
        <label>
          {evidenceRequired ? 'Evidence link' : 'Evidence link (optional)'}
          <input type="url" value={evidence} onChange={event => { setEvidence(event.target.value); setResult(null) }}
            maxLength={2000} required={evidenceRequired} disabled={saving} />
        </label>
        <button type="submit" disabled={!canSubmit}>
          {saving ? 'Posting…' : confirmBlocker ? 'Confirm and return to Ready' : 'Post update'}
        </button>
        {result && <p role={result.error ? 'alert' : 'status'}>{result.message}</p>}
      </form>
    </details>
  )
}
