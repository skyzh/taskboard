import React, { useState } from 'react'
import { queryPath, useLoadedPages, useResource } from './data.js'
import { LoadMore, profileName, ResourceStatus } from './shared.jsx'
import TransitionFeedback, { useTransitionRequest } from './TransitionFeedback.jsx'

const path = '/notifications/preferences/ask-destination'
const labels = { taskboard: 'Taskboard Needs me', raft: 'Raft', local_prompt: 'In-session' }

export default function AskDestinationSettings({ project, revision }) {
  const [agent, selectAgent] = useState('')
  const agents = useLoadedPages(project ? '/assignees' : null, { project }, revision)
  const preference = useResource(queryPath(path, agent ? { project, agent } : {}), revision)
  const mutation = useTransitionRequest(preference.retry)
  if (preference.error?.status === 404 && !agent) return null
  const value = preference.data
  const inherited = agent ? value?.source !== 'agent' : value?.source === 'default'
  return (
    <section className="panel settings-panel" aria-labelledby="ask-destination-heading">
      <h2 id="ask-destination-heading">Incoming agent decisions</h2>
      <p>Choose where agents open a decision for you. Your signed answer is saved on the original ticket.</p>
      <label className="settings-preference">
        Applies to
        <select value={agent} disabled={mutation.saving} onChange={event => selectAgent(event.target.value)}>
          <option value="">Your default</option>
          {(agents.data?.items || []).filter(person => person.kind === 'agent').map(person => (
            <option key={person.id} value={person.id}>{profileName(person.id, person)}</option>
          ))}
        </select>
      </label>
      <LoadMore page={agents} label="project agents" />
      <ResourceStatus resource={preference} label="your decision destination" />
      {value && <label className="settings-preference">
        Preferred destination
        <select value={inherited ? '' : value.destination} disabled={preference.pending || mutation.saving}
          onChange={event => mutation.submit(path, {
            ...(agent ? { project, agent } : {}), destination: event.target.value || null,
          })}>
          <option value="">Use inherited destination ({inherited ? labels[value.destination] : agent ? 'your default' : labels.taskboard})</option>
          {Object.entries(labels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        </select>
      </label>}
      <p>When the preferred route is unavailable, the same decision opens in Taskboard Needs me.</p>
      <TransitionFeedback mutation={mutation} />
    </section>
  )
}
