import React, { Fragment } from 'react'

const boardAndKnowledge = 'Taskboard and Knowledge'

const routes = {
  'task-session': {
    description: 'You start a session for one task. It pulls work and Knowledge and pushes updates directly under your human profile.',
    nodes: ['Session', boardAndKnowledge],
    roles: { Session: 'One task · Human profile' },
    links: [{ twoWay: true, label: 'Pull and push' }],
    note: 'Start the session when you need it. No watcher is needed.',
  },
  'automated-handoff': {
    description: 'A managed Ready watcher detects the Ready ask and wakes an enrolled Raft agent.',
    nodes: ['Ready ask', 'Managed Ready watcher', 'Enrolled Raft agent'],
    roles: { 'Enrolled Raft agent': 'Existing session · Handles the ask' },
    links: [{ label: 'Ready signal' }, { label: 'Wakes' }],
    note: 'The agent reads the linked ask in Taskboard and reports the result on the same ask.',
  },
  'ai-factory': {
    description: 'Taskboard and Knowledge exchange work and results with an enrolled coordinator acting as the DRI, who delegates to existing factory subagents and collects their results.',
    nodes: [boardAndKnowledge, 'Enrolled coordinator', 'Existing workers'],
    roles: { 'Enrolled coordinator': 'DRI · Delegates work', 'Existing workers': 'Factory subagents' },
    links: [{ twoWay: true, label: 'Work and results' }, { twoWay: true, label: 'Tasks and evidence' }],
    note: 'The coordinator uses the factory’s existing worker setup.',
  },
  'local-dri': {
    description: 'You start the registered local runtime, which reads assigned work and Knowledge and writes results to Taskboard.',
    nodes: ['Registered local runtime', boardAndKnowledge],
    roles: { 'Registered local runtime': 'Local DRI session · Owns the ticket' },
    links: [{ twoWay: true, label: 'Read and report' }],
    note: 'Start assigned work locally. Incoming asks from other agents require a running watcher.',
  },
  'team-dri': {
    description: 'A project manager assigns work in Taskboard. The eligible signed Team DRI reads work and Knowledge and reports outcomes to Taskboard.',
    nodes: ['Project manager', 'Taskboard', 'Signed Team DRI'],
    roles: { 'Signed Team DRI': 'Team DRI session · Owns the ticket' },
    links: [{ label: 'Assigns' }, { twoWay: true, label: 'Read and report' }],
    note: 'The agent reads Knowledge as it works.',
  },
}

const localTrial = {
  description: 'The optional Local Codex trial uses a local watcher to start a fresh read-only Codex worker when an approved requester sends a Ready ask.',
  nodes: ['Ready ask', 'Local watcher', 'Read-only Codex worker'],
  roles: { 'Read-only Codex worker': 'Fresh session · One ask' },
  links: [{ label: 'Ready signal' }, { label: 'Starts' }],
}

const localIntake = {
  description: 'A running local watcher detects an incoming Ready ask and starts work in the registered local runtime.',
  nodes: ['Incoming Ready ask', 'Local watcher', 'Local runtime'],
  links: [{ label: 'Ready signal' }, { label: 'Starts work' }],
}

const driDelegation = {
  description: 'If the runtime supports subagents, the DRI session can delegate a step and collect its result while retaining ticket ownership.',
  nodes: ['DRI session', 'Subagents'],
  links: [{ twoWay: true, label: 'Delegate and collect' }],
}

const readyWakes = {
  'ai-factory': {
    description: 'For unattended intake, a Ready watcher notifies the enrolled coordinator.',
    nodes: ['Ready watcher', 'Enrolled coordinator'],
    links: [{ label: 'Wakes' }],
  },
  'team-dri': {
    description: 'Optional assigned Ready wakes notify the Team DRI of unattended work.',
    nodes: ['Assigned Ready wake', 'Team DRI'],
    links: [{ label: 'Notifies' }],
  },
}

function Flow({ route }) {
  return (
    <div className="integration-flow" role="img" aria-label={route.description}>
      {route.nodes.map((node, index) => {
        const link = route.links[index]
        return (
          <Fragment key={node}>
            <div className="integration-flow-node">
              <span>{node}</span>
              {route.roles?.[node] && <small>{route.roles[node]}</small>}
            </div>
            {link && <div className="integration-flow-link">
              <span className="integration-flow-arrow">{link.twoWay ? '⇄' : '→'}</span>
              <span>{link.label}</span>
            </div>}
          </Fragment>
        )
      })}
    </div>
  )
}

export default function IntegrationDiagram({ mode, destination, localRegistrationUnavailable }) {
  const route = routes[mode]
  const readyWake = readyWakes[mode]
  return (
    <figure className="integration-diagram" aria-label="Setup diagram">
      <Flow route={route} />
      <p className="integration-flow-note">{route.note}</p>
      {(mode === 'local-dri' || mode === 'team-dri') && <div className="integration-flow-secondary">
        <h4>Optional delegation</h4>
        <Flow route={driDelegation} />
        <p className="integration-flow-note">If the runtime supports subagents, the DRI can delegate steps and still owns the ticket.</p>
      </div>}
      {readyWake && <div className="integration-flow-secondary">
        <h4>Optional unattended intake</h4>
        <Flow route={readyWake} />
      </div>}
      {mode === 'automated-handoff' && <div className="integration-flow-secondary">
        <h4>Optional Local Codex trial</h4>
        <Flow route={localTrial} />
        <p className="integration-flow-note">{localRegistrationUnavailable
          ? 'This board needs Local DRI registration before it can use the trial.'
          : 'Requires a verified Local DRI in one project, one approved requester, and one authorized checkout.'}</p>
      </div>}
      {mode === 'local-dri' && <div className="integration-flow-secondary">
        <h4>Incoming agent asks</h4>
        <Flow route={localIntake} />
        <p className="integration-flow-note">{destination === 'taskboard'
          ? 'Creator questions go to Taskboard → Notifications → Needs me.'
          : destination === 'local_prompt'
            ? 'Creator questions appear in the local session.'
            : 'Choose whether creator questions appear in the local session or in Taskboard → Notifications → Needs me.'}</p>
      </div>}
    </figure>
  )
}
