import React from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { Markdown, MarkdownInline } from './Markdown.jsx'
import { TaskLink, Timestamp, scheduleHref } from './shared.jsx'

const runLabels = {
  scheduled: 'Scheduled',
  issued: 'Issued',
  started: 'Started',
  completed: 'Completed',
  skipped: 'Skipped',
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

function RunRow({ run, currentTaskId, showOutcome }) {
  const { state, task_id: taskId, task_key: reference, outcome } = run
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
      {showOutcome && outcome && (
        <div className="recurring-run-outcome">
          <p><MarkdownInline text={outcome.note} /></p>
          {outcome.details && <Markdown text={outcome.details} />}
        </div>
      )}
    </li>
  )
}

export function RecurringRunList({ runs, currentTaskId, showOutcome = false }) {
  if (runs.length === 0) return <p>No runs recorded yet.</p>
  return (
    <ol className="recurring-run-list" aria-label="Recurring runs" role="list">
      {runs.map(run => <RunRow key={run.due_at} run={run} currentTaskId={currentTaskId} showOutcome={showOutcome} />)}
    </ol>
  )
}

export default function RecurringRuns({ history }) {
  if (!history) return null
  const current = history.items.find(run => run.task_id === history.current_task_id)
  const runs = [
    ...(current ? [current] : []),
    ...history.items.filter(run => run !== current),
  ]
  return (
    <>
      {history.schedule_id && (
        <p><BoardLink href={scheduleHref(history.schedule_id, history.project)}>Recurring schedule {history.schedule_id}</BoardLink></p>
      )}
      <details className="recurring-runs">
        <summary>Recent runs</summary>
        <RecurringRunList runs={runs} currentTaskId={history.current_task_id} />
      </details>
    </>
  )
}
