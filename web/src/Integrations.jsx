import React, { useState } from 'react'
import { request, useResource } from './data.js'
import { CopyPrompt } from './PersonalCodex.jsx'
import { ResourceStatus, signInHref } from './shared.jsx'

const guide = 'https://github.com/skyzh/taskboard/blob/main/docs/taskboard/PERSONAL-AGENTS.md'

const workflows = [
  {
    id: 'task-session',
    label: 'I open a session for each task',
    access: 'Start with the issue, pull request, or ticket and use the access instructions on this page.',
    prompt: 'Find the existing work and read its status, evidence, and related decisions. Record checkpoints in Activity. At a decision or handoff, update the ticket brief with the current conclusions and links to their sources. Use zero Inbox calls.',
  },
  {
    id: 'assigned-asks',
    label: '1. My personal DRI listens for assigned asks',
    access: 'Start with one manually assigned ask. A listener requires an enrolled Raft agent and its project’s wake sender.',
    prompt: 'Use your own enrolled Raft DRI identity. Read the assigned ask, its source, acceptance criteria, and evidence. Start the ask and return the result or blocker on it. Keep the source ticket’s owner.',
  },
  {
    id: 'task-note',
    label: '2. I share a TaskNote',
    access: 'Attach an authorized HTTPS link to a shared TaskNote. For a note that is only local, use Activity to share its handoff.',
    prompt: 'On the existing ticket, record the conclusion, exact version, evidence, remaining checks, and next action. Link an authorized shared TaskNote if available. Keep current decisions and their sources in the ticket brief, and keep the existing owner.',
  },
  {
    id: 'handoff',
    label: '3. My agents hand off through a shared record',
    access: 'Each participant uses their own account. The request and response stay linked to the same source ticket.',
    prompt: 'Read the shared ticket, TaskNote, evidence, and current decisions. Send a linked ask naming the recipient, expected outcome, evidence, and acceptance criteria. Return the result on that ask and have the requester acknowledge it after applying the result.',
  },
  {
    id: 'team-dri',
    label: '4. My team uses a shared DRI',
    access: 'A project manager selects an enrolled, team-assignable Raft agent. The agent works under its own identity.',
    prompt: 'Read the team assignment under your enrolled Raft DRI identity. Confirm scope, acceptance criteria, and where to escalate. Record the result and evidence on the shared ticket so the requester can resume after signing out.',
  },
]

function WorkflowGuide({ board }) {
  const [selected, setSelected] = useState(workflows[0].id)
  const [publishingInstructions, setPublishingInstructions] = useState('')
  const workflow = workflows.find(item => item.id === selected)
  const workflowPrompt = `Read ${guide} and follow it to use Taskboard at ${board} for this work. ${workflow.prompt}`
  const instructions = publishingInstructions.trim()
  const prompt = instructions
    ? `Apply these publishing instructions before writing to Taskboard. They take precedence over the workflow below. Continue excluded work privately and omit its tickets, updates, and evidence links from Taskboard.\n\n${instructions}\n\n${workflowPrompt}`
    : workflowPrompt

  return (
    <>
      <h3 id="agent-workflow">How do you use your agents?</h3>
      <select className="integration-workflow" aria-labelledby="agent-workflow" value={selected}
        onChange={event => setSelected(event.target.value)}>
        {workflows.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
      </select>
      <p>{workflow.access}</p>
      <label className="integration-publishing-instructions">
        Publishing instructions (optional)
        <textarea rows={3} value={publishingInstructions}
          placeholder="For example, keep security-related work outside Taskboard."
          onChange={event => setPublishingInstructions(event.target.value)} />
      </label>
      <CopyPrompt key={prompt} prompt={prompt} />
      <p className="integration-guide-link"><a href={guide}>Read the Personal Agents guide</a></p>
    </>
  )
}

function CliSignIn({ challenge, session }) {
  const [pending, setPending] = useState(false)
  const [approved, setApproved] = useState(false)
  const [error, setError] = useState(null)
  const human = session.data?.actor?.kind === 'human'
  const signIn = session.data?.sign_in_available || session.error?.status === 401

  async function approve() {
    setPending(true)
    setError(null)
    try {
      await request('/personal-agents/cli-authorize', { body: { challenge } })
      setApproved(true)
    } catch (failure) {
      setError(failure.message)
    } finally {
      setPending(false)
    }
  }

  return (
    <section className="panel settings-panel" aria-labelledby="cli-sign-in">
      <h2 id="cli-sign-in">Approve CLI sign-in</h2>
      {approved ? <p role="status">Approved. Return to the terminal to finish signing in.</p> : <>
        <p>Make sure this code matches your terminal before approving.</p>
        <p className="cli-sign-in-code"><code>{challenge.slice(0, 8).toUpperCase()}</code></p>
        {human ? <button type="button" disabled={pending} onClick={approve}>
          {pending ? 'Approving…' : 'Approve sign-in'}
        </button> : signIn ? <a href={signInHref(`#integrations?cli=${challenge}`)}>Sign in to approve</a>
          : session.pending ? <ResourceStatus resource={session} label="your session" />
            : <p>A signed-in person must approve this request.</p>}
        {error && <p role="alert">{error}</p>}
      </>}
    </section>
  )
}

export default function Integrations({ session, challenge = null }) {
  const access = useResource(challenge === null ? '/personal-agents/access' : null)
  const board = new URL('./', window.location.href).href
  return (
    <section className="settings integrations" aria-labelledby="integrations">
      <div className="page-heading"><h1 id="integrations" tabIndex={-1}>Integrations</h1></div>
      {challenge !== null ? /^[a-f0-9]{64}$/.test(challenge)
        ? <CliSignIn key={`${challenge}:${session.data?.actor?.id || ''}`} challenge={challenge} session={session} />
        : <p role="alert">This sign-in link is invalid. Start sign-in again from the terminal.</p>
        : <section className="panel settings-panel" aria-labelledby="personal-agents">
          <h2 id="personal-agents">Personal Agents</h2>
          {access.data?.mode === 'tailnet'
            ? <p>Use the prompt from a device on this board’s trusted tailnet.</p>
            : <><p>To sign in, run this command and approve it in your browser.</p>
              <p className="cli-login-command"><code>tasks --shared --url {board} login</code></p></>}
          <WorkflowGuide board={board} />
        </section>}
    </section>
  )
}
