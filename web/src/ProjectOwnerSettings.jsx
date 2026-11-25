import React, { useState } from 'react'
import { request, useResource } from './data.js'
import { ResourceStatus } from './shared.jsx'

function label(person) {
  return person.handle ? `${person.name} · @${person.handle}`
    : person.name === person.id ? person.name : `${person.name} · ${person.id}`
}

function ProjectOwners({ project, revision }) {
  const path = `/projects/${encodeURIComponent(project)}/owners`
  const resource = useResource(path, revision)
  const [selected, setSelected] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const { editable, owners = [], choices = [] } = resource.data || {}
  if (resource.error?.status === 404) return null

  async function change(principal, allowed) {
    setBusy(true)
    setResult(null)
    try {
      await request(path, { method: 'PUT', body: { principal, allowed } })
      setSelected('')
      setResult({ message: allowed ? 'Project owner added.' : 'Project owner removed.' })
    } catch (error) {
      setResult({ error: true, message: error.message })
    } finally {
      resource.retry()
      setBusy(false)
    }
  }

  return <>
    <ResourceStatus resource={resource} label="project owners" />
    {resource.data && <>
      {owners.length === 0 && <p>No project owner is registered.</p>}
      <ul className="registration-directory" role="list">
        {owners.map(owner => <li key={owner.id}>
          <span>{label(owner)}</span>
          {editable && owners.length > 1 && <button type="button" aria-label={`Remove ${label(owner)} as owner`}
            disabled={busy || resource.pending} onClick={() => change(owner.id, false)}>Remove</button>}
        </li>)}
      </ul>
      {editable && choices.length > 0 && <form className="ownership-form" onSubmit={event => {
        event.preventDefault()
        if (selected && !busy) change(selected, true)
      }}>
        <label>Add an owner
          <select value={selected} onChange={event => setSelected(event.target.value)} disabled={busy || resource.pending}>
            <option value="">Choose a person or Team DRI</option>
            {choices.map(person => <option key={person.id} value={person.id}>{label(person)}</option>)}
          </select>
        </label>
        <button type="submit" disabled={!selected || busy || resource.pending}>Add owner</button>
      </form>}
    </>}
    {result && <p role={result.error ? 'alert' : 'status'}>{result.message}</p>}
  </>
}

export default function ProjectOwnerSettings({ project, projects, revision }) {
  const [selected, setSelected] = useState(null)
  const scope = projects?.some(item => item.prefix === selected) ? selected : project
  if (!scope) return null
  return <section className="panel settings-panel" aria-labelledby="project-owners-heading">
    <h2 id="project-owners-heading">Project owners</h2>
    {projects?.length > 1 && <label className="settings-preference">Project
      <select value={scope} onChange={event => setSelected(event.target.value)}>
        {projects.map(item => <option key={item.prefix} value={item.prefix}>{item.title}</option>)}
      </select>
    </label>}
    <ProjectOwners key={scope} project={scope} revision={revision} />
  </section>
}
