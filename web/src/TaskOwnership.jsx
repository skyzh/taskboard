import React, { useState } from 'react'
import { useLoadedPages } from './data.js'
import { LoadMore, ResourceStatus } from './shared.jsx'
import TransitionFeedback, { useTransitionRequest } from './TransitionFeedback.jsx'

const labels = { unassign: 'Unassign', reassign: 'Reassign', reopen: 'Reopen' }

export default function TaskOwnership({ task, actions, project, revision, canAssignOnReopen = true, onChanged }) {
  const [open, setOpen] = useState(false)
  const [selection, setSelection] = useState('')
  const [owner, setOwner] = useState('')
  const [reason, setReason] = useState('')
  const mutation = useTransitionRequest(() => {
    setSelection('')
    setOwner('')
    setReason('')
    onChanged()
  }, 'Ownership updated.')

  const allowed = Object.keys(labels).filter(action => actions?.includes(action))
  const action = allowed.includes(selection) ? selection : allowed.length === 1 ? allowed[0] : ''
  const reopening = action === 'reopen'
  const canChooseOwner = action === 'reassign' || (reopening && canAssignOnReopen)
  const assignees = useLoadedPages(open && canChooseOwner ? '/assignees' : null, { project }, revision)
  if (!allowed.length) return null

  const people = (assignees.data?.items || []).flatMap(person => {
    if (!person.id || person.id === task.dri) return []
    const name = [person.name, person.username].find(value => value && !value.startsWith('raft:'))
    return name ? [{ id: person.id, name, kind: person.kind }] : []
  })
  const selectedOwner = canChooseOwner && owner
  const validOwner = selectedOwner ? people.some(person => person.id === selectedOwner) : action !== 'reassign'
  const canSave = Boolean(action && reason.trim() && validOwner)

  function submit(event) {
    event.preventDefault()
    if (!canSave) return
    return mutation.submit(`/tasks/${encodeURIComponent(task.key || task.id)}/ownership`, {
      action, reason: reason.trim(), ...(selectedOwner && { owner: selectedOwner }),
    })
  }

  return (
    <details className="task-ownership" onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>{reopening ? 'Reopen ticket' : 'Change ownership'}</summary>
      <form className="ownership-form" onSubmit={submit}
        data-reload-blocked={Boolean(mutation.blocked || selection || owner || reason)}>
        {!reopening && <label>
          Action
          <select value={action} onChange={event => {
            setSelection(event.target.value)
            setOwner('')
            mutation.clear()
          }} disabled={mutation.blocked} required>
            {allowed.length > 1 && <option value="">Choose an action</option>}
            {allowed.map(option => <option key={option} value={option}>{labels[option]}</option>)}
          </select>
        </label>}
        {action && (reopening || task.state === 'in_progress') && <p>This returns the task to Ready.</p>}
        {canChooseOwner && (
          <>
            <label>
              {reopening ? 'New owner (optional)' : 'New owner'}
              <select value={owner} onChange={event => setOwner(event.target.value)}
                disabled={mutation.blocked || !people.length} required={!reopening}>
                <option value="">{reopening ? (task.dri ? 'Keep current owner' : 'Keep unassigned') : 'Choose a person or DRI'}</option>
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
            disabled={mutation.blocked} required />
        </label>
        <p>The reason is recorded in the task’s activity.</p>
        <button type="submit" disabled={mutation.blocked || !canSave}>
          {mutation.saving ? 'Saving…' : reopening ? 'Reopen ticket' : 'Save ownership change'}
        </button>
        <TransitionFeedback mutation={mutation} />
      </form>
    </details>
  )
}
