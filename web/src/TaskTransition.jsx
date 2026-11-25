import React, { useState } from 'react'
import TransitionFeedback, { useTransitionRequest } from './TransitionFeedback.jsx'

const transitions = {
  start: ['Start work', 'Current step'],
  update: ['Update progress', 'Current step'],
  wait: ['Record a wait', 'Unblock condition'],
  resume: ['Resume work', 'Current step'],
  done: ['Complete ticket', 'Outcome'],
  unpark: ['Return to Ready', 'Next step'],
}

const waitKinds = {
  ticket: 'Blocking ticket',
  teammate: 'Person or DRI',
  automation: 'Automation',
}

export default function TaskTransition({ task, actions, ownsTicket, onChanged }) {
  const [selection, setSelection] = useState('')
  const [note, setNote] = useState('')
  const [waitKind, setWaitKind] = useState('ticket')
  const [dependency, setDependency] = useState('')
  const mutation = useTransitionRequest(() => {
    setSelection('')
    setNote('')
    setDependency('')
    onChanged()
  }, 'Ticket updated.')

  const available = Object.keys(transitions).filter(action => actions?.includes(action))
  if (!available.length) return null
  const action = available.includes(selection) ? selection : available.length === 1 ? available[0] : ''
  const [label, noteLabel] = transitions[action] || []
  const waiting = action === 'wait'
  const allowedWaitKinds = Object.entries(waitKinds).filter(([kind]) => ownsTicket || kind !== 'teammate')
  const ready = Boolean(action && note.trim() && (!waiting || dependency.trim()))

  function submit(event) {
    event.preventDefault()
    if (!ready) return
    const body = { transition: action, note: note.trim() }
    if (waiting) {
      if (waitKind === 'ticket') body.blocked_by = dependency.trim()
      else {
        body.on = dependency.trim()
        if (waitKind === 'automation') body.automation = true
      }
    }
    return mutation.submit(`/tasks/${encodeURIComponent(task.key || task.id)}/transition`, body)
  }

  return (
    <details className="task-ownership">
      <summary>{available.length === 1 ? label : 'Update ticket status'}</summary>
      <form className="ownership-form" onSubmit={submit}
        data-reload-blocked={Boolean(mutation.blocked || selection || note || dependency)}>
        {available.length > 1 && <label>Action
          <select value={action} onChange={event => {
            setSelection(event.target.value)
            setNote('')
            setDependency('')
            mutation.clear()
          }} required disabled={mutation.blocked}>
            <option value="">Choose an action</option>
            {available.map(option => <option key={option} value={option}>{transitions[option][0]}</option>)}
          </select>
        </label>}
        {waiting && <>
          <label>Waiting on
            <select value={waitKind} onChange={event => {
              setWaitKind(event.target.value)
              setDependency('')
            }} disabled={mutation.blocked}>
              {allowedWaitKinds.map(([kind, name]) => <option key={kind} value={kind}>{name}</option>)}
            </select>
          </label>
          <label>{waitKinds[waitKind]}
            <input value={dependency} onChange={event => setDependency(event.target.value)}
              maxLength={waitKind === 'ticket' ? 28 : 160} required disabled={mutation.blocked} />
          </label>
        </>}
        {action && <label>{noteLabel}
          <textarea value={note} onChange={event => setNote(event.target.value)}
            rows={2} maxLength={4000} required disabled={mutation.blocked} />
        </label>}
        <button type="submit" disabled={mutation.blocked || !ready}>{mutation.saving ? 'Saving…' : label || 'Save'}</button>
        <TransitionFeedback mutation={mutation} />
      </form>
    </details>
  )
}
