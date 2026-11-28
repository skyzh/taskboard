import React, { useState } from 'react'
import { request, useLoadedPages } from './data.js'
import { LoadMore, ResourceStatus, Timestamp } from './shared.jsx'
import { useTimeZone } from './timeZone.js'

const statuses = {
  active: 'Active',
  blocked: 'Blocked',
  completed: 'Completed',
  rolled_back: 'Rolled back',
}

function RolloutEditor({ current, task, revision, onChanged }) {
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState(current?.status || 'active')
  const [saving, setSaving] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [result, setResult] = useState(null)
  const [owner, setOwner] = useState(current?.owner || task.dri || '')
  const { timeZone, formatLocalInput } = useTimeZone()
  const people = useLoadedPages(open ? '/assignees' : null, { project: task.key?.split('-')[0] || 'T' }, revision)
  const active = status === 'active' || status === 'blocked'

  async function save(event) {
    event.preventDefault()
    if (saving) return
    const form = new FormData(event.currentTarget)
    const body = Object.fromEntries(form)
    body.expected_event = current?.event_id || 0
    if (active) body.time_zone = timeZone
    if (!body.outcome?.trim()) delete body.outcome
    setSaving(true)
    setResult(null)
    try {
      await request('/tasks/' + encodeURIComponent(task.key || task.id) + '/rollout', { method: 'PUT', body })
      setDirty(false)
      setResult({ message: 'Rollout status saved.' })
      onChanged()
    } catch (error) {
      setResult({ error: true, message: error.message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <details className="task-ownership" onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>{current ? 'Edit rollout status' : 'Track a staged rollout'}</summary>
      <form className="ownership-form" onSubmit={save} onChange={() => setDirty(true)}
        data-reload-blocked={saving || dirty}>
        <label>Status
          <select name="status" value={status} onChange={event => setStatus(event.target.value)} disabled={saving}>
            {Object.entries(statuses).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <label>Rollout owner
          <select name="owner" value={owner} onChange={event => setOwner(event.target.value)} required disabled={saving}>
            <option value="">Choose an owner</option>
            {owner && !people.data?.items.some(person => person.id === owner) && <option value={owner}>{owner}</option>}
            {people.data?.items.map(person => (
              <option key={person.id} value={person.id}>{person.name || person.username || person.id}</option>
            ))}
          </select>
        </label>
        <ResourceStatus resource={people} label="eligible owners" />
        <LoadMore page={people} label="eligible owners" />
        <label>Current audience
          <textarea name="current_audience" defaultValue={current?.current_audience} rows={2} required disabled={saving} />
        </label>
        <label>Intended audience
          <textarea name="intended_audience" defaultValue={current?.intended_audience} rows={2} required disabled={saving} />
        </label>
        <label>Expansion criteria
          <textarea name="expansion_criteria" defaultValue={current?.expansion_criteria} rows={2} required disabled={saving} />
        </label>
        <label>Rollback criteria
          <textarea name="rollback_criteria" defaultValue={current?.rollback_criteria} rows={2} required disabled={saving} />
        </label>
        {active && <label>Next check ({timeZone})
          <input name="at" type="datetime-local" step="1" required disabled={saving}
            defaultValue={current?.next_check_at ? formatLocalInput(current.next_check_at) : ''} />
        </label>}
        {status === 'blocked' && <label>Who can act on the blocker
          <input name="blocker" defaultValue={current?.blocker} required disabled={saving} />
        </label>}
        <label>{status === 'blocked' ? 'Blocker' : 'Outcome'}
          <textarea name="outcome" defaultValue={current?.outcome} rows={2} required={status !== 'active'} disabled={saving} />
        </label>
        {!active && <label>Verification evidence URL
          <input name="url" type="url" maxLength={2000} required disabled={saving} />
        </label>}
        <button type="submit" disabled={saving || !dirty}>{saving ? 'Saving…' : 'Save rollout status'}</button>
        {result && <p role={result.error ? 'alert' : 'status'}>{result.message}</p>}
      </form>
    </details>
  )
}

export default function RolloutStatus({ configuration, task, revision, editing, onChanged }) {
  const current = configuration?.current
  const editable = configuration?.editable && editing
  if (!current && !editable) return null
  return (
    <div>
      <dt>Rollout status</dt>
      <dd>
        {current && <>
          <span>{statuses[current.status]} · {configuration.owner_name || current.owner}</span>
          <dl>
            <div><dt>Current audience</dt><dd>{current.current_audience}</dd></div>
            <div><dt>Intended audience</dt><dd>{current.intended_audience}</dd></div>
            <div><dt>Expand when</dt><dd>{current.expansion_criteria}</dd></div>
            <div><dt>Roll back when</dt><dd>{current.rollback_criteria}</dd></div>
            {current.next_check_at && <div><dt>Next check</dt><dd><Timestamp at={current.next_check_at} /></dd></div>}
            {current.blocker && <div><dt>Blocker owner</dt><dd>{current.blocker}</dd></div>}
            {current.outcome && <div><dt>Outcome</dt><dd>{current.outcome}</dd></div>}
          </dl>
        </>}
        {editable && <RolloutEditor key={current?.event_id || task.id} current={current}
          task={task} revision={revision} onChanged={onChanged} />}
      </dd>
    </div>
  )
}
