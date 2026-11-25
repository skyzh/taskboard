import React from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { ChevronDown } from 'lucide-react'
import { queryPath, useResource } from './data.js'
import { ResourceStatus, TaskLink, Timestamp, eventHref, profileName, taskHref } from './shared.jsx'
import { MarkdownInline } from './Markdown.jsx'

export function goalActivityFragment(project, agent, goal) {
  const params = new URLSearchParams({ view: 'goal-activity', ...(project === null ? { scope: 'all' } : { project }) })
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
        {goal.deadline && <time dateTime={goal.deadline}>{goal.deadline}</time>}
      </p>
      <dl className="goal-activity-counts">
        <div><dt>Starts</dt><dd>{goal.starts}</dd></div>
        <div><dt>Completed checks</dt><dd>{goal.completed_checks}</dd></div>
        {goal.kind === 'standing' && (
          <div><dt>Standing-goal offers</dt><dd>{goal.idle_offers ?? 'Unmeasured'}</dd></div>
        )}
        <div><dt>Last completed check</dt><dd>{goal.last_completed_at
          ? <Timestamp at={goal.last_completed_at} /> : 'None recorded'}</dd></div>
      </dl>
      {goal.outcomes.length > 0 && (
        <ol className="goal-activity-outcomes" aria-label={`Check outcomes for ${goal.key}`} role="list">
          {goal.outcomes.map(outcome => (
            <li key={outcome.id}>
              <BoardLink href={eventHref(outcome.id)}><Timestamp at={outcome.at} /></BoardLink>
              {outcome.note && <span><MarkdownInline text={outcome.note} links={false} /></span>}
            </li>
          ))}
        </ol>
      )}
      {goal.completed_checks > goal.outcomes.length && (
        <p className="report-note">Showing the latest {goal.outcomes.length} checks. <BoardLink href={taskHref(goal.key)}>View the full goal activity</BoardLink></p>
      )}
    </li>
  )
}

export default function GoalActivity({ project, revision, agent, goal, onFilter }) {
  const resource = useResource(queryPath('/reports/goal-activity', project === null ? { scope: 'all' } : { project }), revision)
  const items = resource.data?.items ?? []
  const agents = [...new Map(items.map(item => [item.owner, profileName(item.owner, item.profile)]))]
    .sort((left, right) => left[1].localeCompare(right[1]))
  const selectedAgent = agents.some(([key]) => key === agent) ? agent : ''
  const matchingAgent = items.filter(item => !selectedAgent || item.owner === selectedAgent)
  const selectedGoal = matchingAgent.some(item => item.key === goal) ? goal : ''
  const matchingGoals = matchingAgent.filter(item => !selectedGoal || item.key === selectedGoal)
  const back = project === null ? '#reports-all' : `#reports?${new URLSearchParams({ project })}`

  return (
    <section className="reports goal-activity" aria-labelledby="reports">
      <BoardLink className="back-link" href={back}>Back to Reports</BoardLink>
      <h1 id="reports">Goal activity</h1>
      <p className="goal-activity-description">Taskboard records standing-goal offers here. The watcher records reminder requests. Starts and completed checks come from the goal’s activity.</p>
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
        <p className="section-message" role="status">{project === null ? 'No goals in these projects.' : 'No goals in this project.'}</p>
      )}
    </section>
  )
}
