import React, { useState } from 'react'
import TransitionFeedback, { useTransitionRequest } from './TransitionFeedback.jsx'

export default function TaskClosure({ task, onChanged }) {
  const [reason, setReason] = useState('')
  const [related, setRelated] = useState('')
  const mutation = useTransitionRequest(() => {
    setReason('')
    setRelated('')
    onChanged()
  })

  function submit(event) {
    event.preventDefault()
    if (!reason.trim()) return
    return mutation.submit(`/tasks/${encodeURIComponent(task.key || task.id)}/closure`, {
      reason: reason.trim(), ...(related.trim() && { related: related.trim() }),
    })
  }

  return (
    <details className="task-ownership">
      <summary>Close task</summary>
      <form className="ownership-form" onSubmit={submit}
        data-reload-blocked={Boolean(mutation.blocked || reason || related)}>
        <label>
          Closure reason
          <textarea value={reason} rows={2} maxLength={180} onChange={event => setReason(event.target.value)}
            disabled={mutation.blocked} required />
        </label>
        <label>
          Related ticket (optional)
          <input type="text" value={related} maxLength={28} onChange={event => setRelated(event.target.value)}
            disabled={mutation.blocked} />
        </label>
        <button type="submit" disabled={mutation.blocked || !reason.trim()}>{mutation.saving ? 'Closing…' : 'Close task'}</button>
        <TransitionFeedback mutation={mutation} />
      </form>
    </details>
  )
}
