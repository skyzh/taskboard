import React, { useId, useState } from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { queryPath, request } from './data.js'
import { FieldHelp, useFieldGuidance } from './FieldGuidance.jsx'
import { taskHref, TaskLink } from './shared.jsx'
import { useReportDay } from './ReportTime.jsx'

export function GoalDeadline({ deadline, finished = false }) {
  const today = useReportDay()
  if (!deadline) return null
  const overdue = !finished && deadline < today
  return (
    <span className={overdue ? 'goal-deadline is-overdue' : 'goal-deadline'}>
      <time dateTime={deadline}>{deadline}</time>
      {overdue && <strong>Overdue</strong>}
    </span>
  )
}

function GoalForm({ goal, project, mode, onCancel, onSaved }) {
  const standing = mode === 'standing-goal'
  const ticketsHelp = useId()
  const titleHelpId = useId()
  const [summary, setSummary] = useState(goal?.summary || '')
  const [deadline, setDeadline] = useState(goal?.deadline || '')
  const savedTickets = (goal?.tickets || []).map(ticket => ticket.key).join(', ')
  const [tickets, setTickets] = useState(savedTickets)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  const { data: fieldGuidance } = useFieldGuidance(project)
  const goalField = {
    guidance: fieldGuidance?.fields?.title?.guidance,
    format: 'Plain text. Markdown and Mermaid are displayed as written.',
  }

  async function save(value) {
    if (saving) return
    setSaving(true)
    setError(null)
    try {
      const path = `/profile/${mode}`
      const body = standing ? { summary: value?.summary ?? null } : { goal: value }
      if (mode === 'milestone' && goal) body.previous = goal.summary
      await request(queryPath(path, { project }), { method: 'PATCH', body })
      onSaved()
    } catch (failure) {
      setError(failure.message)
    } finally {
      setSaving(false)
    }
  }

  function submit(event) {
    event.preventDefault()
    const references = tickets.split(',').map(ticket => ticket.trim()).filter(Boolean)
    save({ summary, deadline, tickets: references })
  }

  const edited = summary !== (goal?.summary || '') || deadline !== (goal?.deadline || '') || tickets !== savedTickets
  return (
    <form className="profile-form dri-goal-form" onSubmit={submit} data-reload-blocked={edited || saving}>
      <label>{standing ? 'Standing goal' : 'Milestone name'}<input value={summary} required disabled={saving}
        aria-describedby={titleHelpId}
        onChange={event => setSummary(event.target.value)} /></label>
      <FieldHelp field={goalField} id={titleHelpId} />
      {!standing && <>
        <label>Deadline<input type="date" value={deadline} required disabled={saving}
          onChange={event => setDeadline(event.target.value)} /></label>
        <label>Related task IDs<input value={tickets} maxLength={1200} disabled={saving} aria-describedby={ticketsHelp}
          onChange={event => setTickets(event.target.value)} /></label>
        <p id={ticketsHelp} className="section-message">Enter IDs of tasks you own in this project, separated by commas.</p>
      </>}
      <div className="dri-goal-actions">
        <button disabled={saving}>{saving ? 'Saving…' : 'Save goal'}</button>
        <button type="button" onClick={onCancel} disabled={saving}>Cancel</button>
        {goal && <button type="button" onClick={() => save(null)} disabled={saving}>Clear goal</button>}
      </div>
      {error && <p role="alert">{error}</p>}
    </form>
  )
}

function GoalDetails({ goal }) {
  return (
    <>
      <p className="section-message">Deadline <GoalDeadline deadline={goal.deadline} /></p>
      {goal.tickets.length > 0 && (
        <ul className="context-list" aria-label="Related tasks">
          {goal.tickets.map(ticket => <li key={ticket.id}><TaskLink id={ticket.id} reference={ticket.key} title={ticket.title} /></li>)}
        </ul>
      )}
    </>
  )
}

export default function DriGoal({ goal, project, standing = false, editable, onChanged }) {
  const [editing, setEditing] = useState(false)
  const heading = standing ? 'dri-standing-goal-title' : 'dri-goal-title'
  const label = standing ? 'Standing goal' : 'Original dated goal'

  function saved() {
    setEditing(false)
    onChanged()
  }

  return (
    <section className="dri-goal" aria-labelledby={heading}>
      <div className="dri-goal-actions">
        <h2 id={heading}>{label}</h2>
        {editable && !editing && (
          <button type="button" onClick={() => setEditing(true)} aria-label={`${goal ? 'Edit' : 'Set'} ${label.toLowerCase()}`}>
            {goal ? 'Edit goal' : 'Set goal'}
          </button>
        )}
      </div>
      {goal ? (
        <>
          <p>{standing && goal.ticket ? <BoardLink href={taskHref(goal.ticket)}>{goal.summary}</BoardLink> : goal.summary}</p>
          {!standing && <GoalDetails goal={goal} />}
        </>
      ) : <p className="section-message">No goal set.</p>}
      {editable && editing && <GoalForm goal={goal} project={project} mode={standing ? 'standing-goal' : 'goal'} onCancel={() => setEditing(false)} onSaved={saved} />}
    </section>
  )
}

export function DriMilestones({ milestones, project, editable, onChanged }) {
  const [editing, setEditing] = useState(null)

  function saved() {
    setEditing(null)
    onChanged()
  }

  const form = goal => <GoalForm goal={goal} project={project} mode="milestone"
    onCancel={() => setEditing(null)} onSaved={saved} />

  return (
    <section className="dri-goal" aria-labelledby="dri-milestones-title">
      <div className="dri-goal-actions">
        <h2 id="dri-milestones-title">Dated milestones</h2>
        {editable && editing === null && <button type="button" onClick={() => setEditing('')}>Add milestone</button>}
      </div>
      {milestones.map(goal => (
        <div className="dri-milestone" key={goal.ticket || goal.summary}>
          <div className="dri-goal-actions">
            <h3>{goal.ticket ? <BoardLink href={taskHref(goal.ticket)}>{goal.summary}</BoardLink> : goal.summary}</h3>
            {editable && editing === null && (
              <button type="button" aria-label={`Edit ${goal.summary}`} onClick={() => setEditing(goal.summary)}>Edit</button>
            )}
          </div>
          <GoalDetails goal={goal} />
          {editing === goal.summary && form(goal)}
        </div>
      ))}
      {editing === '' && form(null)}
    </section>
  )
}
