import React, { useState } from 'react'
import { request } from './data.js'
import { taskHref } from './shared.jsx'

export default function TaskProjectMove({ task, destinations, onMoved }) {
  const [project, setProject] = useState('')
  const [moving, setMoving] = useState(false)
  const [error, setError] = useState(null)
  const allowed = destinations.some(destination => destination.prefix === project)

  async function submit(event) {
    event.preventDefault()
    if (moving || !allowed) return
    setMoving(true)
    setError(null)
    try {
      const { key, receipt } = await request(`/tasks/${encodeURIComponent(task.key)}/project`, {
        method: 'POST', body: { project },
      })
      onMoved(taskHref(task.id, key), receipt)
    } catch (failure) {
      setError(failure.message)
    } finally {
      setMoving(false)
    }
  }

  return (
    <details className="task-ownership">
      <summary>Move to another project</summary>
      <form className="ownership-form" onSubmit={submit} data-reload-blocked={Boolean(moving || project)}>
        <label>
          Destination project
          <select value={project} onChange={event => { setProject(event.target.value); setError(null) }}
            disabled={moving} required>
            <option value="">Choose a project</option>
            {destinations.map(({ prefix, title }) => <option key={prefix} value={prefix}>{title} ({prefix})</option>)}
          </select>
        </label>
        {error && <p role="alert">{error}</p>}
        <button type="submit" disabled={!allowed} aria-disabled={moving || undefined}>
          {moving ? 'Moving…' : 'Move ticket'}
        </button>
      </form>
    </details>
  )
}
