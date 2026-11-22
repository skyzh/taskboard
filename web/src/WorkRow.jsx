import React, { useState } from 'react'
import { useResource } from './data.js'
import { Assignee, ChecklistSteps, ClosureReference, Dependency, EvidenceLinks, PeerRequestContext, ReminderWhen, ResourceStatus, Status, TaskLink, TaskPriority, Timestamp, eventHref, taskLabel, taskState } from './shared.jsx'
import { ReportTime } from './ReportTime.jsx'
import { reportTaskCue } from './workCopy.js'
import { Markdown, MarkdownInline } from './Markdown.jsx'
import { TaskWaitAge } from './WaitAge.jsx'

export function ChecklistProgress({ task }) {
  const total = task.checklist_total
  const completed = task.checklist_completed
  if (!total) return null
  return (
    <span className="task-progress">
      <progress value={completed} max={total} aria-label={`Checklist for ${taskLabel(task.id, task.key)}`}
        aria-valuetext={`${completed} of ${total} complete`} />
      <span aria-hidden="true">{completed}/{total}</span>
    </span>
  )
}

function RowChecklist({ task, revision }) {
  const resource = useResource(`/tasks/${task.key || task.id}`, revision)
  return (
    <>
      <ResourceStatus resource={resource} label="checklist" />
      {resource.data && <ChecklistSteps steps={resource.data.checklist} />}
    </>
  )
}

const releaseLabels = {
  released: 'Released',
  rolled_back: 'Rolled back',
  rejected: 'Release rejected',
  rollback_failed: 'Rollback failed',
}

function ReportReleaseResult({ receipt }) {
  const label = receipt.status === 'released' && receipt.release_number != null
    ? `Released in #${receipt.release_number}`
    : releaseLabels[receipt.status]
  if (!label) return null
  return (
    <p className="event-meta">
      <strong>{label}</strong>
      <Timestamp at={receipt.at} />
      {receipt.receipt_url && <a href={receipt.receipt_url}>Release receipt</a>}
    </p>
  )
}

export function TaskRow({ task, owner = true, selected = false, report = false, ownReport = false, revision,
  compactQueue = false, searchQuery }) {
  const [showChecklist, setShowChecklist] = useState(false)
  const checklistId = `row-checklist-${task.id}`
  const cue = report ? reportTaskCue(task) : null
  const reportSummary = report === 'summary'
  const detailedReport = report && !reportSummary
  const state = taskState(task)
  const showRequestState = !reportSummary || task.peer_request?.phase !== 'in_progress'
  const showTaskStatus = !reportSummary || !task.peer_request || !showRequestState
  const headingStatus = showTaskStatus && reportSummary && ['waiting', 'in_progress'].includes(state)
  const activityAt = task.last_activity_at || task.at
  const detailNote = detailedReport && (cue ? cue.details : task.note)
  const showDetails = detailedReport && (detailNote || task.links?.length > 0 || task.event_id)
  return (
    <li className={`task-row${selected ? ' selected' : ''}${report ? ' report-task' : ''}`}>
      <TaskPriority priority={task.priority} strip />
      <div className="task-row-heading">
        {headingStatus && <Status state={state} compactOnPhone />}
        <TaskLink reference={task.key} id={task.id} title={task.title} current={selected} searchQuery={searchQuery} />
        <div className="task-row-meta">
          {compactQueue && <span className="queue-task-key ticket-id" aria-hidden="true">{taskLabel(task.id, task.key)}</span>}
          {showTaskStatus && !headingStatus && <Status state={state} />}
          {!reportSummary && <TaskWaitAge task={task} />}
          {owner && <Assignee task={task} compact />}
          {compactQueue && ['high', 'low'].includes(task.priority) && (
            <span className="queue-task-priority"><TaskPriority priority={task.priority} /></span>
          )}
          {detailedReport && activityAt && <span>Last activity <ReportTime at={activityAt} /></span>}
        </div>
      </div>
      <div className="task-row-context">
        <PeerRequestContext task={task} showAskedAge={reportSummary} showState={showRequestState} />
        {task.waiting_on && <p className="task-dependency"><Dependency task={task} /></p>}
        {cue?.text && (
          <div className="report-task-cue">
            {detailedReport && cue.label && <strong>{cue.label}</strong>}
            <Markdown text={cue.text} className="task-row-note prose" />
            {detailedReport && task.release_result && task.at && <p className="event-meta">Step updated <ReportTime at={task.at} /></p>}
          </div>
        )}
        {detailedReport && task.release_result && <ReportReleaseResult receipt={task.release_result} />}
        {detailedReport && cue?.text && ownReport && task.release_after_step && (
          <p className="report-note">Review your next step after this release result.</p>
        )}
        {!report && task.note && <p className="task-row-note"><MarkdownInline text={task.note} /></p>}
        <ClosureReference context={task.context} />
        <ReminderWhen context={task.context} className="task-dependency" />
        {showDetails && (
          <details className="report-details">
            <summary aria-label={`Update and evidence for ${taskLabel(task.id, task.key)}`}>Update and evidence</summary>
            {detailNote && <Markdown text={detailNote} />}
            <EvidenceLinks links={task.links} />
            {task.event_id && <a href={eventHref(task.event_id)}>Open activity entry</a>}
          </details>
        )}
        {!report && <EvidenceLinks links={task.links} />}
      </div>
      {report && task.checklist_total > 0 ? (
        <>
          <button type="button" className="task-checklist" aria-expanded={showChecklist} aria-controls={checklistId}
            aria-label={`Checklist for ${taskLabel(task.id, task.key)}, ${task.checklist_completed} of ${task.checklist_total} complete`}
            onClick={() => setShowChecklist(open => !open)}>
            <ChecklistProgress task={task} />
          </button>
          <div className="task-checklist-steps" id={checklistId} hidden={!showChecklist}>
            {showChecklist && <RowChecklist task={task} revision={revision} />}
          </div>
        </>
      ) : <ChecklistProgress task={task} />}
    </li>
  )
}

export function TaskRows({ items, owner = true, selected, label, report, ownReport, revision, compactQueue = false,
  listRef, onClick, searchQuery }) {
  if (!items?.length) return null
  return (
    <ol ref={listRef} onClick={onClick} className={compactQueue ? 'task-rows compact-queue-rows' : 'task-rows'}
      aria-label={label} role="list">
      {items.map(task => (
        <TaskRow key={task.id} task={task} owner={owner} selected={task.id === selected || task.key === selected}
          report={report} ownReport={ownReport} revision={revision} compactQueue={compactQueue} searchQuery={searchQuery} />
      ))}
    </ol>
  )
}
