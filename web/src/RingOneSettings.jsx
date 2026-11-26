import React, { useRef, useState } from 'react'
import { request, useResource } from './data.js'
import { ResourceStatus, Timestamp } from './shared.jsx'

const identityKinds = { human: 'Human', team_dri: 'Team DRI', local_dri: 'Local DRI', agent: 'Agent' }

function identityLabel(person) {
  return [person.name, identityKinds[person.kind], person.handle && `@${person.handle}`].filter(Boolean).join(' · ')
}

export default function RingOneSettings({ project, revision, onChanged }) {
  const path = `/projects/${encodeURIComponent(project)}/ring1`
  const access = useResource(path, revision)
  const heading = useRef(null)
  const [choice, setChoice] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [accessRemoved, setAccessRemoved] = useState(false)
  const data = access.data?.project === project ? access.data : null
  const selected = data?.choices.find(person => person.id === choice)
  const disabled = busy || access.pending
  const people = new Map(data ? [...data.members, ...data.choices].map(person => [person.id, person]) : [])

  function historyName(id) {
    const person = people.get(id)
    return person ? identityLabel(person) : id
  }

  async function change(person, allowed) {
    if (disabled) return
    setBusy(true)
    setResult(null)
    try {
      const response = await request(path, { method: 'PUT', body: { principal: person.id, allowed } })
      if (!allowed && response.receipt && !response.project) setAccessRemoved(true)
      heading.current?.focus()
      if (allowed) setChoice('')
      setResult({ message: allowed ? `Added ${person.name} to Ring 1.` : `Removed Ring 1 access for ${person.name}.` })
      onChanged()
    } catch (error) {
      setResult({ error: true, message: error.message })
      access.retry()
    } finally {
      setBusy(false)
    }
  }

  if (accessRemoved || [401, 403, 404].includes(access.error?.status)) return null

  return (
    <section className="ring-one-settings" aria-labelledby="ring-one-access">
      <h3 id="ring-one-access" ref={heading} tabIndex={-1}>Ring 1 access</h3>
      <ResourceStatus resource={access} label="Ring 1 access" />
      {data && <>
        <ul className="ring-one-members" aria-label="Ring 1 members" role="list">
          {data.members.map(person => (
            <li className="permission-row" key={person.id}>
              <span>{identityLabel(person)}</span>
              {person.removable === true ? (
                <button type="button" aria-label={`Remove Ring 1 access for ${identityLabel(person)}`}
                  disabled={disabled} onClick={() => change(person, false)}>Remove access</button>
              ) : <span className="report-note">Project owner</span>}
            </li>
          ))}
        </ul>
        {data.choices.length > 0 && (
          <form className="ownership-form" onSubmit={event => {
            event.preventDefault()
            if (selected) change(selected, true)
          }}>
            <label>Add a person or agent
              <select value={selected?.id || ''} onChange={event => setChoice(event.target.value)} disabled={disabled} required>
                <option value="">Choose a verified person or agent</option>
                {data.choices.map(person => <option key={person.id} value={person.id}>{identityLabel(person)}</option>)}
              </select>
            </label>
            <button type="submit" disabled={disabled || !selected}>Add member</button>
          </form>
        )}
        {data.history.length > 0 && (
          <details className="ring-one-history">
            <summary>Membership history</summary>
            <ol>
              {data.history.map(event => (
                <li key={`${event.at}:${event.actor}:${event.operation}:${event.principal}`}>
                  <span>{historyName(event.actor)} {event.operation === 'grant' ? 'added' : 'removed'} {historyName(event.principal)}</span>
                  <Timestamp at={event.at} />
                </li>
              ))}
            </ol>
          </details>
        )}
      </>}
      {result && <p role={result.error ? 'alert' : 'status'}>{result.message}</p>}
    </section>
  )
}
