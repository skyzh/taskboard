import React, { useState } from 'react'
import { useLoadedPages } from './data.js'
import { LoadMore, ResourceStatus, TaskLink, profileName } from './shared.jsx'
import TransitionFeedback, { useTransitionRequest } from './TransitionFeedback.jsx'

export default function PeerAskSend({ ticket, allowed, actor, project, revision, onChanged }) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [recipient, setRecipient] = useState('')
  const [note, setNote] = useState('')
  const [receipt, setReceipt] = useState('')
  const mutation = useTransitionRequest(result => {
    setReceipt(result?.receipt || 'Ask sent.')
    setQuery('')
    setRecipient('')
    setNote('')
    onChanged()
  })
  const page = useLoadedPages(open && allowed ? '/assignees' : null, { project, query: query.trim() }, revision)
  const agents = (page.data?.items || []).filter(person =>
    person.kind === 'agent' && person.id && person.id !== actor && person.accepts_agent_asks === true)
  const ready = agents.some(person => person.id === recipient) && note.trim()
  const [reference] = receipt.split(' · ')
  const linked = reference !== receipt

  function submit(event) {
    event.preventDefault()
    if (!ready || !allowed) return
    setReceipt('')
    mutation.submit(`/tasks/${encodeURIComponent(ticket)}/peer-ask`, { recipient, note: note.trim() })
  }

  return <>
    {allowed && <details className="task-ownership needs-you-send" onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>Ask an agent</summary>
      <form className="ownership-form" onSubmit={submit}
        data-reload-blocked={Boolean(mutation.blocked || recipient || note)}>
        <label>Find an agent
          <input type="search" maxLength={100} value={query} disabled={mutation.blocked}
            onChange={event => {
              setQuery(event.target.value)
              setRecipient('')
              mutation.clear()
            }} />
        </label>
        <label>Agent
          <select required value={recipient} onChange={event => {
            setRecipient(event.target.value)
            mutation.clear()
          }} disabled={mutation.blocked || !agents.length}>
            <option value="">Choose an agent</option>
            {agents.map(person => <option key={person.id} value={person.id}>{profileName(person.id, person)}</option>)}
          </select>
        </label>
        <ResourceStatus resource={page} label="agents" />
        {!page.pending && page.data && !agents.length && page.data.next_cursor == null &&
          <p className="section-message">No eligible agents found.</p>}
        <LoadMore page={page} label="agents" />
        <label>What should the agent do?
          <textarea required rows={3} value={note} disabled={mutation.blocked}
            onChange={event => { setNote(event.target.value); mutation.clear() }} />
        </label>
        <button type="submit" disabled={mutation.blocked || !ready}>{mutation.saving ? 'Sending…' : 'Send ask'}</button>
        <TransitionFeedback mutation={mutation} />
      </form>
    </details>}
    {receipt && <p role="status">{linked ? <>Agent ask <TaskLink reference={reference} /> is in the agent’s Inbox.</> : receipt}</p>}
  </>
}
