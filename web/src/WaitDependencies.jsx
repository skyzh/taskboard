import React from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { useResource } from './data.js'
import { projectHref } from './projectScope.js'
import { ResourceStatus, Status, useSignInHref } from './shared.jsx'
import './dependency-view.css'

const dependencyLabels = {
  ticket: 'Blocking ticket',
  agent: 'Agent action',
  question: 'Question',
  unavailable: 'Prerequisite unavailable',
  legacy: 'No verified prerequisite',
}

function groupDependencies(items) {
  const groups = new Map()
  for (const item of items) {
    const { source, wait } = item
    const target = ['ticket', 'agent', 'question'].includes(item.kind) ? item.target : null
    const reference = target?.event_id ?? target?.id ?? target?.key
    const kind = reference == null ? (item.kind === 'legacy' ? 'legacy' : 'unavailable') : item.kind
    const key = `${kind}:${reference ?? source.key}`
    if (!groups.has(key)) groups.set(key, { key, kind, target: reference == null ? null : target, waiting: [] })
    groups.get(key).waiting.push({ source, wait })
  }
  const order = Object.keys(dependencyLabels)
  return [...groups.values()].sort((left, right) => order.indexOf(left.kind) - order.indexOf(right.kind))
}

function DependencyName({ record }) {
  const name = record.title || record.name || record.key
  const label = <>{record.key && record.key !== name && <span className="ticket-id">{record.key}</span>} {name}</>
  return record.url ? <BoardLink href={record.url}>{label}</BoardLink> : <strong>{label}</strong>
}

function TicketMeta({ record }) {
  if (!record.state) return null
  return <p className="dependency-meta"><Status state={record.state} />{record.owner?.name ?? 'No visible owner'}</p>
}

function DependencyGroup({ group }) {
  return (
    <li className="dependency-group">
      <div className="dependency-node dependency-prerequisite">
        <h2>{dependencyLabels[group.kind]}</h2>
        {group.target && <>
          <p><DependencyName record={group.target} /></p>
          <TicketMeta record={group.target} />
        </>}
      </div>
      <ul className="dependency-waiting" aria-label="Waiting tickets" role="list">
        {group.waiting.map(({ source, wait }) => <li className="dependency-node" key={wait?.event_id ?? source.key}>
          <p><DependencyName record={source} /></p>
          <TicketMeta record={source} />
          {wait?.url ? <BoardLink className="dependency-wait-link" href={wait.url}
            aria-label={`Wait activity for ${source.key}`}>Wait activity</BoardLink>
            : <p className="dependency-meta">Wait activity unavailable</p>}
        </li>)}
      </ul>
    </li>
  )
}

export default function WaitDependencies({ project, revision, session }) {
  const actor = session.data?.actor
  const signed = ['agent', 'human'].includes(actor?.kind)
  const login = useSignInHref(projectHref('#reports?view=dependencies', project))
  const page = useResource(signed && project ? `/projects/${encodeURIComponent(project)}/dependency-edges` : null, revision)
  const groups = groupDependencies(page.data?.items ?? [])
  return (
    <section className="feed-section wait-dependencies" aria-labelledby="wait-dependencies">
      <div className="page-heading">
        <h1 id="wait-dependencies">Dependencies</h1>
        <BoardLink href={projectHref('#reports', project)}>Back to Reports</BoardLink>
      </div>
      {signed && page.error?.status !== 401 ? <ResourceStatus resource={page} label="dependencies" />
        : (session.data?.sign_in_available || page.error?.status === 401 || session.error?.status === 401)
          ? <p className="section-message"><a href={login}>Sign in with Raft to view dependencies</a></p>
          : <p className="section-message">Dependencies are available from the signed-in board.</p>}
      {signed && page.data && (groups.length > 0 ? <>
        <p className="dependency-guide">Each prerequisite points to the tickets waiting on it.</p>
        <ul className="dependency-groups" aria-label="Dependency graph" role="list">
          {groups.map(group => <DependencyGroup key={group.key} group={group} />)}
        </ul>
      </> : <p className="section-message">No waiting dependencies in this project.</p>)}
    </section>
  )
}
