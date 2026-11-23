import React from 'react'
import { ChevronDown } from 'lucide-react'
import { queryPath, useResource } from './data.js'
import { ResourceStatus, TaskLink, Timestamp, eventHref, profileName, taskHref } from './shared.jsx'
import { GoalDeadline } from './DriGoal.jsx'
import { MarkdownInline } from './Markdown.jsx'

export function goalActivityFragment(project, agent, goal) {
  const params = new URLSearchParams({ view: 'goal-activity', project })
  if (agent) params.set('agent', agent)
  if (goal) params.set('goal', goal)
  return `reports?${params}`
}

function GoalActivityRow({ goal }) {
  const name = profileName(goal.owner, goal.profile)
  return (
    <li className="goal-activity-row">
      <h2><TaskLink reference={goal.key} title={goal.summary} /></h2>
      <p className="goal-activity-context">
        <span>{name}</span>
        <span>{goal.kind === 'standing' ? 'Standing goal' : 'Dated goal'}</span>
        {goal.deadline && <GoalDeadline deadline={goal.deadline} />}
      </p>
      <dl className="goal-activity-counts">
        <div><dt>Starts</dt><dd>{goal.starts}</dd></div>
        <div><dt>Completed checks</dt><dd>{goal.completed_checks}</dd></div>
        <div><dt>Idle offers</dt><dd>{goal.idle_offers ?? 'Unmeasured'}</dd></div>
        <div><dt>Last completed check</dt><dd>{goal.last_completed_at
          ? <Timestamp at={goal.last_completed_at} /> : 'None recorded'}</dd></div>
      </dl>
      {goal.outcomes.length > 0 && (
        <ol className="goal-activity-outcomes" aria-label={`Check outcomes for ${goal.key}`} role="list">
          {goal.outcomes.map(outcome => (
            <li key={outcome.id}>
              <a href={eventHref(outcome.id)}><Timestamp at={outcome.at} /></a>
              {outcome.note && <span><MarkdownInline text={outcome.note} links={false} /></span>}
            </li>
          ))}
        </ol>
      )}
      {goal.completed_checks > goal.outcomes.length && (
        <p className="report-note">Showing the latest {goal.outcomes.length} checks. <a href={taskHref(goal.key)}>View the full goal activity</a></p>
      )}
    </li>
  )
}

export default function GoalActivity({ project, revision, agent, goal, onFilter }) {
  const resource = useResource(queryPath('/reports/goal-activity', { project }), revision)
  const items = resource.data?.items ?? []
  const agents = [...new Map(items.map(item => [item.owner, profileName(item.owner, item.profile)]))]
    .sort((left, right) => left[1].localeCompare(right[1]))
  const selectedAgent = agents.some(([key]) => key === agent) ? agent : ''
  const matchingAgent = items.filter(item => !selectedAgent || item.owner === selectedAgent)
  const selectedGoal = matchingAgent.some(item => item.key === goal) ? goal : ''
  const matchingGoals = matchingAgent.filter(item => !selectedGoal || item.key === selectedGoal)
  const back = `#reports?${new URLSearchParams({ project })}`

  return (
    <section className="reports goal-activity" aria-labelledby="reports">
      <a className="back-link" href={back}>Back to Reports</a>
      <h1 id="reports">Goal activity</h1>
      <p className="goal-activity-description">Activity records starts and completed checks. Idle offers count recorded handoffs when an agent was idle.</p>
      <ResourceStatus resource={resource} label="goal activity" />
      {items.length > 0 && (
        <>
          <div className="goal-activity-filters">
            <label>Agent<span className="embedded-picker">
              <select value={selectedAgent} onChange={event => onFilter(event.target.value, '')}>
                <option value="">All agents</option>
                {agents.map(([key, name]) => <option key={key} value={key}>{name}</option>)}
              </select>
              <ChevronDown size={14} aria-hidden="true" focusable="false" />
            </span></label>
            <label>Goal<span className="embedded-picker">
              <select value={selectedGoal} onChange={event => onFilter(selectedAgent, event.target.value)}>
                <option value="">All goals</option>
                {matchingAgent.map(item => <option key={item.key} value={item.key}>{item.key} {item.summary}</option>)}
              </select>
              <ChevronDown size={14} aria-hidden="true" focusable="false" />
            </span></label>
          </div>
          <ul className="goal-activity-list" role="list" aria-label="Goals">
            {matchingGoals.map(item => <GoalActivityRow key={item.key} goal={item} />)}
          </ul>
        </>
      )}
      {resource.data && !resource.pending && !resource.error && items.length === 0 && (
        <p className="section-message" role="status">No goals in this project.</p>
      )}
    </section>
  )
}
