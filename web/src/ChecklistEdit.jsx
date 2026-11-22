import React, { useState } from 'react'
import { request } from './data.js'

export default function ChecklistEdit({ task, step, onChanged }) {
  const [draft, setDraft] = useState(null)
  const [saving, setSaving] = useState(false)
  const [failure, setFailure] = useState(null)
  const label = draft ?? step?.label ?? ''
  const rename = Boolean(step)
  const complete = Boolean(step?.checked)
  const unchanged = label.trim() === (step?.label ?? '')
  const path = `/tasks/${encodeURIComponent(task.key || task.id)}/checklist`

  async function save(action, fields = {}) {
    setSaving(true)
    setFailure(null)
    try {
      await request(path, {
        method: 'POST',
        body: { action, ...(step && { position: step.position }), ...fields },
      })
      setDraft(null)
      onChanged()
    } catch (error) {
      setFailure(error.message)
    } finally {
      setSaving(false)
    }
  }

  function submit(event) {
    event.preventDefault()
    if (label.trim() && !unchanged && !saving) save(rename ? 'rename' : 'add', { label: label.trim() })
  }

  return (
    <details className="checklist-edit task-ownership">
      <summary>{rename ? `Edit step ${step.position}` : 'Add checklist step'}</summary>
      <form className="ownership-form" onSubmit={submit} data-reload-blocked={saving || draft !== null}>
        <label>
          Step wording
          <input value={label} onChange={event => setDraft(event.target.value)}
            maxLength={240} disabled={saving} required />
        </label>
        <button type="submit" disabled={!label.trim() || unchanged || saving}>
          {rename ? 'Save wording' : 'Add step'}
        </button>
        {complete && <p>Keep completed steps unless the work has changed.</p>}
        {complete && <button type="button" disabled={saving} onClick={() => save('reopen')}>Reopen step</button>}
        {step && <button type="button" disabled={saving} onClick={() => save('remove')}>Remove step</button>}
        {failure && <p role="alert">{failure}</p>}
      </form>
    </details>
  )
}
