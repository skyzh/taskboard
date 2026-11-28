import React, { useState } from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { queryPath, useLoadedPages, useResource } from './data.js'
import { projectHref } from './projectScope.js'
import { LoadMore, ResourceStatus, TaskLink, Timestamp, eventHref } from './shared.jsx'

function WatcherRow({ row, project }) {
  const watcher = row.watcher
  const state = watcher?.state === 'running' ? 'Running' : 'Exited'
  const agent = new URLSearchParams({ dri: row.agent, project })
  return (
    <li className="event-row">
      <p className="event-ticket"><BoardLink href={`#${agent}`}>{row.name}</BoardLink></p>
      {watcher ? <>
        <p className="event-meta">
          <strong>{watcher.stale ? `Stale · last reported ${state.toLowerCase()}` : state}</strong>
          <span>Last report <Timestamp at={watcher.reported_at} /></span>
        </p>
        <dl className="notification-log-times">
          <div><dt>Revision</dt><dd>{watcher.revision ? <code title={watcher.revision}>{watcher.revision.slice(0, 12)}</code> : 'Unavailable'}</dd></div>
          <div><dt>Ready</dt><dd>{watcher.ready ? 'On' : 'Off'}</dd></div>
          <div><dt>Goal</dt><dd>{watcher.goals ? 'On' : 'Off'}</dd></div>
          <div><dt>Stalled work</dt><dd>{watcher.stalled ? 'On' : 'Off'}</dd></div>
        </dl>
      </> : <p className="event-meta">Watcher report unavailable</p>}
      <p className="event-meta">Last verified pickup{' '}
        {row.pickup ? <BoardLink href={projectHref(eventHref(row.pickup.event_id), project)}>
          {row.pickup.ticket} · <Timestamp at={row.pickup.at} />
        </BoardLink> : 'Unknown'}
      </p>
    </li>
  )
}

const workLabels = {
  ready: 'Ready to start',
  review: 'Independent review',
  report: 'Collect a worker report',
  readiness: 'Readiness check',
  request: 'Request',
  decision: 'Decision requested',
  reminder: 'Reminder due',
  in_progress: 'Continue work in progress',
  shared_ready: 'Unassigned Ready work',
  worker_checkpoint: 'Worker checkpoint',
  goal_review: 'Goal review due',
  'wait-review': 'Wait review due',
  'resolved-wait': 'Dependency resolved',
  ask_recheck: 'Request recheck due',
}

function InboxRows({ label, items }) {
  if (!items.length) return null
  return (
    <section aria-label={label}>
      <h3>{label}</h3>
      <ol className="event-list">
        {items.map(({ key, ticket, title, label: name, at, href }) => (
          <li className="event-row" key={key}>
            <p className="event-ticket"><TaskLink reference={ticket} title={title} /></p>
            <p className="event-meta">
              {href ? <BoardLink href={href}>{name}</BoardLink> : name}
              {at && <Timestamp at={at} />}
            </p>
          </li>
        ))}
      </ol>
    </section>
  )
}

function AgentInbox({ agent, project, revision }) {
  const page = useLoadedPages('/reports/agent-inbox', { agent, project }, revision)
  const inbox = page.data
  return (
    <>
      <ResourceStatus resource={page} label="agent Inbox" />
      {inbox && <>
        <h3>{inbox.agent.name}’s Inbox</h3>
        <InboxRows label="Goal work" items={inbox.goal_occurrences.map(item => ({
          key: item.key, ticket: item.ticket, label: item.kind === 'review' ? 'Scheduled Review' : 'Explore',
          at: item.due_at,
        }))} />
        <InboxRows label="Actionable work" items={inbox.work.map(item => ({
          key: `${item.kind}:${item.key}`, ticket: item.key, title: item.title,
          label: workLabels[item.kind] || 'Actionable work',
        }))} />
        <InboxRows label="Review waiting work" items={inbox.wait_review.map(item => ({
          key: item.wait_event, ticket: item.key, title: item.title, label: 'Waiting activity',
          href: projectHref(eventHref(item.wait_event), project),
        }))} />
        <InboxRows label="Inbox updates" items={inbox.items.map(item => ({
          ...item, key: `${item.kind}:${item.id}`,
          href: ['watched', 'decision'].includes(item.kind) ? projectHref(eventHref(item.id), project) : undefined,
        }))} />
        <LoadMore page={page} label="Inbox updates" />
        <InboxRows label="Standing goals" items={inbox.standing_goals.map(goal => ({
          key: goal.ticket, ticket: goal.ticket, title: goal.summary, label: 'Standing goal',
        }))} />
        <InboxRows label="Dated goals" items={inbox.dated_goals.map(goal => ({
          key: goal.ticket, ticket: goal.ticket, title: goal.summary, label: 'Dated goal',
        }))} />
        {['work', 'goal_occurrences', 'wait_review', 'items', 'standing_goals', 'dated_goals']
          .every(field => inbox[field].length === 0) && <p className="section-message">This Inbox is empty.</p>}
      </>}
    </>
  )
}

function AgentInboxPicker({ project, revision }) {
  const [agent, setAgent] = useState('')
  const roster = useResource(queryPath('/reports/agent-inbox', { project }), revision)
  const agents = roster.data?.agents || []
  const selected = agents.some(person => person.id === agent) ? agent : ''
  return (
    <section className="feed-section" aria-labelledby="agent-inbox-title">
      <h2 id="agent-inbox-title">Agent Inbox</h2>
      <p>Read-only view for the selected project. You stay signed in as yourself.</p>
      <ResourceStatus resource={roster} label="agents for this project" />
      {roster.data && (agents.length > 0 ? (
        <label>
          Agent{' '}
          <select value={selected} onChange={event => setAgent(event.currentTarget.value)}>
            <option value="">Choose an agent</option>
            {agents.map(person => <option key={person.id} value={person.id}>{person.name}</option>)}
          </select>
        </label>
      ) : <p className="section-message">No agents can access this project.</p>)}
      {selected && <AgentInbox key={selected} agent={selected} project={project} revision={revision} />}
    </section>
  )
}

export default function AgentManagement({ project, revision, canViewInbox }) {
  const [refresh, setRefresh] = useState(0)
  const health = useResource(project ? queryPath('/reports/watchers', { project }) : null, `${revision}:${refresh}`)
  const rows = health.data?.items || []
  return (
    <section className="feed-section" aria-labelledby="agent-management">
      <h1 id="agent-management">Agent management</h1>
      {canViewInbox && <AgentInboxPicker project={project} revision={revision} />}
      <h2>Watcher reports</h2>
      <p>Watcher reports describe the managed watcher process. Runtime activity and ticket status are separate.</p>
      <button type="button" onClick={() => setRefresh(value => value + 1)} disabled={!project || health.pending}>Refresh watcher reports</button>
      <ResourceStatus resource={health} label="watcher reports" />
      {health.data && rows.length === 0 && <p className="section-message">No agents in this project.</p>}
      {rows.length > 0 && (
        <ul className="event-list" aria-label="Agent watchers">
          {rows.map(row => <WatcherRow row={row} project={project} key={row.agent} />)}
        </ul>
      )}
    </section>
  )
}
