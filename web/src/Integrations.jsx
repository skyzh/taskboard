import React, { useState } from 'react'
import { useResource } from './data.js'
import { CopyPrompt, personalCodexStatus, SessionConnections } from './PersonalCodex.jsx'
import { ResourceStatus, signInHref } from './shared.jsx'

const path = '/personal-agents/connections'
const guide = 'https://github.com/skyzh/taskboard/blob/main/docs/taskboard/PERSONAL-AGENTS.md'
const environment = 'TASKBOARD_PERSONAL_AGENT_TOKEN'

const workflows = [
  {
    id: 'task-session',
    label: 'I open a session for each task',
    access: 'Use a Personal Agents connection under your signed-in profile. Work starts with the issue, pull request, or ticket.',
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

function WorkflowGuide() {
  const [selected, setSelected] = useState(workflows[0].id)
  const workflow = workflows.find(item => item.id === selected)
  const base = new URL('./', window.location.href).href
  const prompt = `Read ${guide} and follow it to use Taskboard at ${base} for this work. ${workflow.prompt}`

  return (
    <>
      <h3 id="agent-workflow">How do you use your agents?</h3>
      <select className="integration-workflow" aria-labelledby="agent-workflow" value={selected}
        onChange={event => setSelected(event.target.value)}>
        {workflows.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
      </select>
      <p>{workflow.access}</p>
      <CopyPrompt key={selected} prompt={prompt} />
      <p className="integration-guide-link"><a href={guide}>Read the Personal Agents guide</a></p>
    </>
  )
}

export default function Integrations({ session }) {
  const actor = session.data?.actor
  const canConnect = actor?.kind === 'human'
  const connections = useResource(canConnect ? path : null)
  const pilot = useResource(canConnect ? personalCodexStatus : null)
  const signIn = (!actor || actor.kind === 'portal') &&
    (session.data?.sign_in_available || session.error?.status === 401)

  return (
    <section className="settings integrations" aria-labelledby="integrations">
      <div className="page-heading"><h1 id="integrations" tabIndex={-1}>Integrations</h1></div>
      <section className="panel settings-panel" aria-labelledby="personal-agents">
        <h2 id="personal-agents">Personal Agents</h2>
        <WorkflowGuide />
      </section>
      {canConnect ? (
        <>
          <ResourceStatus resource={connections} label="session connections" />
          {connections.data && ![401, 403, 404].includes(connections.error?.status) && (
            <SessionConnections connections={connections.data.connections} reload={connections.retry}
              path={path} tokenVariable={environment}
              expiration="Connections use your Taskboard permissions and expire after eight hours or when you sign out, whichever comes first." />
          )}
          {pilot.data && !pilot.error && <p className="integration-guide-link"><a href="#personal-codex">Open the single-ticket pilot</a></p>}
        </>
      ) : signIn ? <p className="integration-guide-link">
        <a href={signInHref('#integrations')}>Sign in to create a connection</a>
      </p> : <ResourceStatus resource={session} label="your session" />}
    </section>
  )
}
