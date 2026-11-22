import React, { useState } from 'react'
import { request } from './data.js'

export default function TaskClosure({ task, onChanged }) {
  const [reason, setReason] = useState('')
  const [related, setRelated] = useState('')
  const [saving, setSaving] = useState(false)
  const [failure, setFailure] = useState(null)

  async function submit(event) {
    event.preventDefault()
    if (saving || !reason.trim()) return
    setSaving(true)
    setFailure(null)
    try {
      await request(`/tasks/${encodeURIComponent(task.key || task.id)}/closure`, {
        method: 'POST',
        body: { reason: reason.trim(), ...(related.trim() && { related: related.trim() }) },
      })
      setReason('')
      setRelated('')
      onChanged()
    } catch (error) {
      setFailure(error.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <details className="task-ownership">
      <summary>Close task</summary>
      <form className="ownership-form" onSubmit={submit}
        data-reload-blocked={Boolean(saving || reason || related)}>
        <label>
          Closure reason
          <textarea value={reason} rows={2} maxLength={180} onChange={event => setReason(event.target.value)}
            disabled={saving} required />
        </label>
        <label>
          Related ticket (optional)
          <input type="text" value={related} maxLength={28} onChange={event => setRelated(event.target.value)}
            disabled={saving} />
        </label>
        {failure && <p role="alert">{failure}</p>}
        <button type="submit" disabled={saving || !reason.trim()}>{saving ? 'Closing…' : 'Close task'}</button>
      </form>
    </details>
  )
}
