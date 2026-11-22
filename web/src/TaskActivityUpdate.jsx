import React, { useState } from 'react'
import { request } from './data.js'

export default function TaskActivityUpdate({ task, onChanged }) {
  const [note, setNote] = useState('')
  const [evidence, setEvidence] = useState('')
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState(null)

  async function submit(event) {
    event.preventDefault()
    if (!note.trim() || saving) return
    setSaving(true)
    setResult(null)
    try {
      await request(`/tasks/${encodeURIComponent(task.key || task.id)}/activity`, {
        body: { note: note.trim(), ...(evidence.trim() && { url: evidence.trim() }) },
      })
      setNote('')
      setEvidence('')
      onChanged()
      setResult({ message: 'Update added.' })
    } catch (error) {
      setResult({ error: true, message: error.message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <details className="task-ownership">
      <summary>Add update</summary>
      <form className="ownership-form" onSubmit={submit} data-reload-blocked={Boolean(saving || note || evidence)}>
        <label>
          Update
          <textarea value={note} onChange={event => { setNote(event.target.value); setResult(null) }}
            rows={3} maxLength={4000} required disabled={saving} />
        </label>
        <label>
          Evidence link (optional)
          <input type="url" value={evidence} onChange={event => { setEvidence(event.target.value); setResult(null) }}
            maxLength={2000} disabled={saving} />
        </label>
        <button type="submit" disabled={!note.trim() || saving}>{saving ? 'Posting…' : 'Post update'}</button>
        {result && <p role={result.error ? 'alert' : 'status'}>{result.message}</p>}
      </form>
    </details>
  )
}
