import React, { useState } from 'react'
import { request, useLoadedPages } from './data.js'
import { LoadMore, ResourceStatus } from './shared.jsx'

const labels = { unassign: 'Unassign', reassign: 'Reassign', reopen: 'Reopen' }

export default function TaskOwnership({ task, actions, project, revision, onChanged }) {
  const [open, setOpen] = useState(false)
  const [selection, setSelection] = useState('')
  const [owner, setOwner] = useState('')
  const [reason, setReason] = useState('')
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState(null)

  const allowed = Object.keys(labels).filter(action => actions?.includes(action))
  const action = allowed.includes(selection) ? selection : allowed.length === 1 ? allowed[0] : ''
  const needsOwner = action === 'reassign' || action === 'reopen'
  const assignees = useLoadedPages(open && needsOwner ? '/assignees' : null, { project }, revision)
  if (!allowed.length) return null

  const people = (assignees.data?.items || []).flatMap(person => {
    if (!person.id || (action === 'reassign' && person.id === task.dri)) return []
    const name = [person.name, person.username].find(value => value && !value.startsWith('raft:'))
    return name ? [{ id: person.id, name, kind: person.kind }] : []
  })
  const canSave = Boolean(action && reason.trim() && (!needsOwner || people.some(person => person.id === owner)))

  async function submit(event) {
    event.preventDefault()
    if (saving || !canSave) return
    setSaving(true)
    setNotice(null)
    try {
      await request(`/tasks/${encodeURIComponent(task.key || task.id)}/ownership`, {
        method: 'POST',
        body: { action, reason: reason.trim(), ...(needsOwner && { owner }) },
      })
      setSelection('')
      setOwner('')
      setReason('')
      setNotice({ text: 'Ownership updated.' })
      onChanged()
    } catch (error) {
      setNotice({ error: true, text: error.message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <details className="task-ownership" onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>Change ownership</summary>
      <form className="ownership-form" onSubmit={submit}
        data-reload-blocked={Boolean(saving || selection || owner || reason)}>
        <label>
          Action
          <select value={action} onChange={event => {
            setSelection(event.target.value)
            setNotice(null)
          }} disabled={saving} required>
            {allowed.length > 1 && <option value="">Choose an action</option>}
            {allowed.map(option => <option key={option} value={option}>{labels[option]}</option>)}
          </select>
        </label>
        {action && (action === 'reopen' || task.state === 'in_progress') && <p>This returns the task to Ready.</p>}
        {needsOwner && (
          <>
            <label>
              New owner
              <select value={owner} onChange={event => setOwner(event.target.value)}
                disabled={saving || !people.length} required>
                <option value="">Choose a person or DRI</option>
                {people.map(person => (
                  <option key={person.id} value={person.id}>
                    {person.name} ({person.kind === 'human' ? 'Person' : 'DRI'})
                  </option>
                ))}
              </select>
            </label>
            <ResourceStatus resource={assignees} label="eligible owners" />
            {assignees.data && !people.length && assignees.data.next_cursor == null && (
              <p className="section-message">No eligible owners are available.</p>
            )}
            <LoadMore page={assignees} label="eligible owners" />
          </>
        )}
        <label>
          Reason for the change
          <textarea value={reason} rows={3} maxLength={4000} onChange={event => setReason(event.target.value)}
            disabled={saving} required />
        </label>
        <p>The reason is recorded in the task’s activity.</p>
        <button type="submit" disabled={saving || !canSave}>{saving ? 'Saving…' : 'Save ownership change'}</button>
        {notice && <p role={notice.error ? 'alert' : 'status'}>{notice.text}</p>}
      </form>
    </details>
  )
}
