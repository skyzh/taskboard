import React, { useRef, useState } from 'react'
import { useLoadedPages } from './data.js'
import { LoadMore, ResourceStatus, profileName } from './shared.jsx'
import TransitionFeedback, { useTransitionRequest } from './TransitionFeedback.jsx'

const labels = { decision: 'Decision', attention: 'Attention request', fyi: 'FYI' }

export default function NeedsYouSend({ ticket, actions, replacement, project, revision, onChanged }) {
  const [open, setOpen] = useState(false)
  const [selection, setSelection] = useState('')
  const [recipient, setRecipient] = useState('')
  const [message, setMessage] = useState('')
  const [choices, setChoices] = useState('')
  const pendingAttention = useRef(null)
  const mutation = useTransitionRequest(() => {
    pendingAttention.current = null
    setSelection('')
    setRecipient('')
    setMessage('')
    setChoices('')
    onChanged()
  }, 'Needs You ask sent.')

  const available = Object.keys(labels).filter(kind => actions?.includes(kind === 'fyi' ? 'attention' : kind))
  const replacing = Boolean(replacement)
  const kind = replacing ? 'replace-attention'
    : available.includes(selection) ? selection : available.length === 1 ? available[0] : ''
  const assignees = useLoadedPages(open && !replacing && available.length ? '/assignees' : null, { project }, revision)
  if (!replacing && !available.length) return null

  const people = (assignees.data?.items || []).filter(person => person.id && person.kind === 'human')
  const decision = kind === 'decision' || replacing
  const text = message.trim()
  const ready = kind && text && (replacing || people.some(person => person.id === recipient))

  function submit(event) {
    event.preventDefault()
    if (!ready) return
    const taskPath = `/tasks/${encodeURIComponent(ticket)}`
    const path = `${taskPath}/needs-you`
    if (decision) {
      const options = choices.split('\n').map(choice => choice.trim()).filter(Boolean)
      const question = { note: text, ...(options.length && { options }) }
      if (replacing) {
        const { request_id, wait_event, recipient } = replacement
        return mutation.submit(`${taskPath}/request-question`, { ...question, request_id, wait_event, recipient })
      }
      return mutation.submit(path, { ...question, kind, on: recipient })
    }
    const payload = JSON.stringify([kind, recipient, text])
    if (pendingAttention.current?.payload !== payload) {
      pendingAttention.current = { payload, key: crypto.randomUUID() }
    }
    return mutation.submit(path, { kind, reason: text, recipient, request_key: pendingAttention.current.key })
  }

  return (
    <details className="task-ownership needs-you-send" onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>{replacing ? 'Replace request with a Question' : 'Send Needs You ask'}</summary>
      <form className="ownership-form" onSubmit={submit}
        data-reload-blocked={Boolean(open && (mutation.blocked || selection || recipient || message || choices))}>
        {available.length > 1 && <label>Ask type
          <select required value={kind} onChange={event => {
            setSelection(event.target.value)
            mutation.clear()
          }} disabled={mutation.blocked}>
            <option value="">Choose an ask type</option>
            {available.map(option => <option key={option} value={option}>{labels[option]}</option>)}
          </select>
        </label>}
        {replacing ? <>
          <p>The new Question goes to {replacement.recipient_name} and closes this attention request.</p>
          <blockquote>{replacement.reason}</blockquote>
        </> : <>
          {kind && <p>{decision
            ? 'The ticket waits for the recipient’s answer.'
            : kind === 'fyi' ? 'The recipient can acknowledge the update.'
              : 'The recipient gets an attention request. The ticket keeps its current status.'}</p>}
          <label>Recipient
            <select required value={recipient} onChange={event => {
              setRecipient(event.target.value)
              mutation.clear()
            }} disabled={mutation.blocked || !people.length}>
              <option value="">Choose a person</option>
              {people.map(person => <option key={person.id} value={person.id}>
                {profileName(person.id, person)}
              </option>)}
            </select>
          </label>
          <ResourceStatus resource={assignees} label="recipients" />
          {assignees.data && !people.length && assignees.data.next_cursor == null &&
            <p className="section-message">No people who have signed in are available.</p>}
          <LoadMore page={assignees} label="recipients" />
        </>}
        <label>{decision ? 'Decision question' : kind ? 'Reason for the request' : 'Message'}
          <textarea required rows={3} value={message} disabled={mutation.blocked}
            onChange={event => { setMessage(event.target.value); mutation.clear() }} />
        </label>
        {decision && <label>Choices (optional, one per line)
          <textarea rows={2} value={choices} disabled={mutation.blocked}
            onChange={event => { setChoices(event.target.value); mutation.clear() }} />
        </label>}
        <button type="submit" disabled={mutation.blocked || !ready}>
          {mutation.saving ? 'Sending…' : replacing ? 'Replace request and send Question'
            : decision ? 'Request decision' : kind === 'fyi' ? 'Send FYI' : kind ? 'Request attention' : 'Send ask'}
        </button>
        <TransitionFeedback mutation={mutation} />
      </form>
    </details>
  )
}
