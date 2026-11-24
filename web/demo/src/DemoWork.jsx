import React, { useState } from 'react'
import { DemoUnavailable } from '../../src/DemoControl.jsx'
import { Markdown } from '../../src/Markdown.jsx'
import SegmentedControl from '../../src/SegmentedControl.jsx'
import {
  Assignee, ChecklistSteps, Options, PeerRequestContext, Status, TaskLink, eventHref, states,
} from '../../src/shared.jsx'
import { ActivityEntry, EventRow } from '../../src/Work.jsx'
import { TaskRows } from '../../src/WorkRow.jsx'
import { checkedNames, decision, events, knowledge, knowledgeHref, project, review, tasks } from './fixture.js'

export function TaskPage({ task, backHref, backLabel }) {
  const [showActions, setShowActions] = useState(false)
  const activity = events.filter(event => event.task_id === task.id)
  return (
    <article className="panel task-detail" aria-labelledby="demo-page-title">
      <div className="ticket-heading-meta">
        <a className="back-link" href={backHref}>{backLabel}</a>
        <p className="detail-meta"><span className="ticket-id">{task.key}</span></p>
        <DemoUnavailable className="history-link">History</DemoUnavailable>
        <button className="ticket-actions-toggle" type="button" aria-expanded={showActions}
          aria-controls="demo-ticket-actions" onClick={() => setShowActions(value => !value)}>Ticket actions</button>
      </div>
      <h1 id="demo-page-title" tabIndex={-1}>{task.title}</h1>
      <div className="ticket-primary-identity">
        <div className="detail-owner"><Assignee task={task} /></div>
        <div className="detail-meta"><Status state={task.state} /></div>
      </div>
      <div id="demo-ticket-actions" className="demo-actions" hidden={!showActions}>
        <DemoUnavailable>Edit title and brief</DemoUnavailable>
        <DemoUnavailable>Watch task</DemoUnavailable>
        <DemoUnavailable>Record progress</DemoUnavailable>
        {task.state === 'human_review' && <DemoUnavailable>Answer decision</DemoUnavailable>}
      </div>
      <section className={`ticket-focus ${task.state}`} aria-label={task.summary_label}>
        <h2>{task.summary_label}</h2>
        <PeerRequestContext task={task} />
        <Markdown text={task.note} />
        {task.context?.choices && (task.state === 'done'
          ? <details><summary>Original options</summary><Options options={task.context.choices} /></details>
          : <Options options={task.context.choices} />)}
      </section>
      {task.brief && <section className="detail-brief">
        <h2>Description</h2><Markdown text={task.brief} className="prose" />
      </section>}
      {task.checklist?.length > 0 && <details className="detail-checklist" open>
        <summary><span>Checklist</span><span>{task.checklist_completed} of {task.checklist_total} complete</span></summary>
        <ChecklistSteps steps={task.checklist} />
      </details>}
      {task === decision && <section className="ticket-context-content">
        <h2>Related records</h2>
        <ul className="context-list">
          <li><TaskLink reference={review.key} id={review.id} title={review.title} /></li>
          <li><a href={knowledgeHref(knowledge.note.key)}>{knowledge.note.title}</a></li>
        </ul>
      </section>}
      <section className="ticket-activity" aria-labelledby="demo-ticket-activity">
        <h2 id="demo-ticket-activity">Activity</h2>
        <ol className="event-list" role="list">
          {activity.map(event => <EventRow key={event.id} event={event} ticketActivity />)}
        </ol>
      </section>
    </article>
  )
}

export function ActivityPage({ selected, agent = false }) {
  const [filter, setFilter] = useState('')
  const items = agent ? events.filter(event => [review.id, checkedNames.id].includes(event.task_id)) : events
  const visible = items.filter(event => selected ? event.id === selected : !filter || event.actor === filter)
  return (
    <section className="panel" aria-labelledby="demo-page-title">
      {selected && <a className="back-link" href="#activity">All activity</a>}
      <div className="page-heading">
        <h1 id="demo-page-title" tabIndex={-1}>{agent ? 'My activity' : 'Activity'}</h1>
        {!selected && !agent && <label className="demo-field">
          Person
          <select value={filter} onChange={event => setFilter(event.target.value)}>
            <option value="">Everyone</option>
            <option value="avery">Avery</option>
            <option value="juniper">Juniper</option>
          </select>
        </label>}
      </div>
      {agent && <>
        <p>Juniper’s agent view shows activity on assigned or watched work.</p>
        <TaskRows items={[review]} label="Work assigned to Juniper" />
        <h2>Recent activity</h2>
      </>}
      <ol className="event-list" role="list">{visible.map(event => <ActivityEntry key={event.id} event={event} full selected={event.id === selected} />)}</ol>
    </section>
  )
}

const layouts = [{ value: 'list', label: 'List' }, { value: 'kanban', label: 'Kanban' }]
const columns = ['ready', 'in_progress', 'human_review', 'done']

export function TasksPage({ layout, chooseLayout, column, chooseColumn, owner, query }) {
  const filter = query?.toLowerCase().trim()
  const items = tasks.filter(task => (!owner || task.dri === owner) &&
    (!filter || `${task.key} ${task.title} ${task.brief || ''}`.toLowerCase().includes(filter)))
  return (
    <section aria-labelledby="demo-page-title">
      <div className="page-heading tasks-heading">
        <h1 id="demo-page-title" tabIndex={-1}>{query ? 'Search results' : 'Tasks'}</h1>
        {!query && <a href={`#schedules=&project=${project.key}`}>Schedules</a>}
      </div>
      {query && <p>Tickets matching “{query}”</p>}
      {owner && <p><a href="#board">Clear owner filter</a></p>}
      {!query && <SegmentedControl label="Task view" options={layouts} value={layout} onChange={chooseLayout} />}
      {!items.length && <p role="status">No matching tickets.</p>}
      {query || layout === 'list' ? <TaskRows items={items} label="Inventory Export tasks" compactQueue /> : <>
        <label className="kanban-column-picker demo-field">Kanban column
          <select aria-label="Kanban column" value={column} onChange={event => chooseColumn(event.target.value)}>
            {columns.map(state => <option key={state} value={state}>{states[state]}</option>)}
          </select>
        </label>
        <div className="kanban-columns multiple demo-kanban" role="region" aria-label="Kanban columns" tabIndex={0}>
          {columns.map(state => (
            <section className="kanban-column" key={state} data-selected={state === column} aria-labelledby={'demo-column-' + state}>
              <h2 id={'demo-column-' + state}>{states[state]}</h2>
              <TaskRows items={items.filter(task => task.state === state)} owner={false} label={states[state]} />
            </section>
          ))}
        </div>
      </>}
    </section>
  )
}

export function EventLink({ event, children }) {
  return <a href={eventHref(event.id)}>{children || event.label}</a>
}
