import React from 'react'
import { TaskLink, Timestamp } from './shared.jsx'

const runLabels = {
  scheduled: 'Scheduled',
  issued: 'Issued',
  started: 'Started',
  completed: 'Completed',
  missed: 'Missed',
  closed: 'Closed',
}

function RunTime({ label, value, missing }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value ? <Timestamp at={value} /> : missing}</dd>
    </div>
  )
}

function RunRow({ run, currentTaskId }) {
  const { state, task_id: taskId, task_key: reference } = run
  const current = taskId != null && taskId === currentTaskId
  const terminal = state === 'completed' || state === 'closed'
  return (
    <li>
      <p className="recurring-run-heading">
        <strong>{runLabels[state] || 'Status unavailable'}</strong>
        {taskId != null && <TaskLink id={taskId} reference={reference} current={current} />}
        {current && <span>This ticket</span>}
      </p>
      <dl className="recurring-run-times">
        <RunTime label="Due" value={run.due_at} missing="Unavailable" />
        <RunTime label="Issued" value={run.issued_at} missing={taskId == null ? 'Not issued' : 'Unavailable'} />
        <RunTime label="Started" value={run.started_at}
          missing={state === 'started' || terminal ? 'Unavailable' : 'Not started'} />
        <RunTime label={state === 'closed' ? 'Closed' : 'Finished'} value={run.terminal_at}
          missing={terminal ? 'Unavailable' : taskId == null ? 'Not applicable' : 'No outcome yet'} />
      </dl>
    </li>
  )
}

export default function RecurringRuns({ history }) {
  if (!history) return null
  const runs = history.next_at
    ? [{ state: 'scheduled', due_at: history.next_at }, ...history.items]
    : history.items
  return (
    <details className="recurring-runs">
      <summary>Recent runs</summary>
      {runs.length === 0 && <p>No runs recorded yet.</p>}
      {runs.length > 0 && (
        <ol aria-label="Recurring runs" role="list">
          {runs.map(run => <RunRow key={run.due_at} run={run} currentTaskId={history.current_task_id} />)}
        </ol>
      )}
    </details>
  )
}
