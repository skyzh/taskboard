import React, { useState } from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { Reply } from 'lucide-react'
import { useResource } from './data.js'
import { Assignee, ChecklistSteps, ClosureReference, Dependency, EvidenceLinks, PeerRequestContext, ReminderWhen, ResourceStatus, Status, TaskLink, TaskPriority, Timestamp, eventHref, profileName, taskLabel, taskState } from './shared.jsx'
import { ReportTime } from './ReportTime.jsx'
import { reportTaskCue } from './workCopy.js'
import { Markdown, MarkdownInline } from './Markdown.jsx'
import { TaskWaitAge } from './WaitAge.jsx'

export function ChecklistProgress({ task }) {
  const total = task.checklist_total
  const completed = task.checklist_completed
  if (!total) return null
  const scanning = completed > 0 && completed < total
  return (
    <span className="task-progress">
      <span className="task-progress-bar">
        <progress value={completed} max={total} aria-label={`Checklist for ${taskLabel(task.id, task.key)}`}
          aria-valuetext={`${completed} of ${total} complete`} />
        {scanning && <span className="task-progress-glow" aria-hidden="true"
          style={{ width: `${completed / total * 100}%` }} />}
      </span>
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
      {receipt.receipt_url && <BoardLink href={receipt.receipt_url}>Release receipt</BoardLink>}
    </p>
  )
}

function ReportPeerRequester({ ask, parked }) {
  const requester = profileName(ask.requester, { name: ask.requester_name })
  return (
    <p className="report-peer-request">
      <Reply size={14} aria-hidden="true" focusable="false" />
      {ask.phase === 'asked' || parked ? `Request from ${requester}` : `Responding to ${requester}’s request`}
      {ask.phase === 'asked' && ask.asked_at && <> <ReportTime at={ask.asked_at} /></>}
    </p>
  )
}

export function TaskRow({ task, owner = true, selected = false, report = false, ownReport = false, revision,
  compactQueue = false, searchQuery, publicPreview = false, projectLabel }) {
  const [showChecklist, setShowChecklist] = useState(false)
  const checklistId = `row-checklist-${task.id}`
  const reportSummary = report === 'summary'
  const detailedReport = report && !reportSummary && !publicPreview
  const state = taskState(task)
  const ask = publicPreview ? undefined : task.peer_request
  const showRequester = reportSummary && ['asked', 'in_progress'].includes(ask?.phase)
  const parkedRequest = showRequester && state === 'parked'
  const cue = report ? reportTaskCue(task, parkedRequest) : null
  const needsDecision = reportSummary && !publicPreview && task.summary_needs_decision
  const currentWork = report && (state === 'in_progress' || state === 'waiting' || needsDecision)
  const showTaskStatus = !reportSummary || !ask || ask.phase === 'in_progress' || parkedRequest
  const dependency = task.waiting_on || task.blockers
  const exceptionalPriority = task.priority === 'high' || task.priority === 'low'
  const activityAt = task.last_activity_at || task.at
  const showReportedProgress = report && !publicPreview
    && (reportSummary || state === 'in_progress') && task.reported_progress !== undefined
  const progressReport = task.reported_progress
  const detailNote = detailedReport && (cue ? cue.details : task.note)
  const showDetails = detailedReport && (detailNote || task.links?.length > 0 || task.event_id)
  const cueLabel = publicPreview ? task.summary_label : detailedReport && cue?.label
  const step = cue?.text && (
    <div className="report-task-cue">
      {cueLabel && <strong>{cueLabel}</strong>}
      <Markdown text={cue.text} className="task-row-note prose" links={!publicPreview} />
      {detailedReport && task.release_result && task.at && <p className="event-meta">Step updated <ReportTime at={task.at} /></p>}
    </div>
  )
  const lastActivity = detailedReport && activityAt && !showReportedProgress &&
    <span>Last activity <ReportTime at={activityAt} /></span>
  const reportTime = showReportedProgress && (
    <p className="event-meta">
      {progressReport
        ? <>Progress last reported <BoardLink href={eventHref(progressReport.id)}><ReportTime at={progressReport.at} linked /></BoardLink></>
        : 'Progress report time unknown'}
      {task.local_watcher === 'ready_only' && <> · Watcher handles Ready asks only</>}
      {task.local_watcher === 'off' && <> · Automatic follow-up off</>}
    </p>
  )
  const context = (
    <>
      {currentWork && step}
      {showRequester && currentWork && <ReportPeerRequester ask={ask} parked={parkedRequest} />}
      {!publicPreview && <PeerRequestContext task={task} showState={!showRequester} />}
      {currentWork && lastActivity && <p className="event-meta">{lastActivity}</p>}
      {currentWork && !reportSummary && <TaskWaitAge task={task} />}
      {reportTime}
      {dependency && (
        <p className="task-dependency"><Dependency task={task} publicPreview={publicPreview} /></p>
      )}
      {needsDecision && <p className="task-dependency">Waiting on <strong>you</strong></p>}
      {!currentWork && step}
      {detailedReport && task.release_result && <ReportReleaseResult receipt={task.release_result} />}
      {detailedReport && cue?.text && ownReport && task.release_after_step && (
        <p className="report-note">Review your next step after this release result.</p>
      )}
      {!report && task.note && (state === 'human_review'
        ? <Markdown text={task.note} className="task-decision-note prose" />
        : <p className="task-row-note"><MarkdownInline text={task.note} /></p>)}
      {!publicPreview && <>
        <ClosureReference context={task.context} />
        <ReminderWhen context={task.context} className="task-dependency" />
      </>}
      {showDetails && (
        <details className="report-details">
          <summary aria-label={`Update and evidence for ${taskLabel(task.id, task.key)}`}>Update and evidence</summary>
          {detailNote && <Markdown text={detailNote} />}
          <EvidenceLinks links={task.links} />
          {task.event_id && <BoardLink href={eventHref(task.event_id)}>Open activity entry</BoardLink>}
        </details>
      )}
      {!report && <EvidenceLinks links={task.links} />}
    </>
  )
  return (
    <li className={`task-row${selected ? ' selected' : ''}${report ? ' report-task' : ''}${reportSummary ? ' report-task-summary' : ''}${currentWork ? ' report-current-work' : ''}`}>
      {currentWork && <div className="report-work-status"><Status state={state} /></div>}
      <div className="task-row-heading">
        {!reportSummary && <TaskPriority priority={task.priority} strip />}
        {showRequester && !currentWork && <ReportPeerRequester ask={ask} parked={parkedRequest} />}
        <TaskLink reference={task.key} id={task.id} title={task.title} current={selected} searchQuery={searchQuery}
          publicHref={publicPreview ? task.public_url || null : undefined} />
        {reportSummary && (exceptionalPriority
          ? <TaskPriority priority={task.priority} short />
          : <span className="report-priority-accessible">Priority Normal</span>)}
        <div className="task-row-meta">
          {projectLabel && <span className="report-project-label">{projectLabel}</span>}
          {compactQueue && <span className="queue-task-key ticket-id" aria-hidden="true">{taskLabel(task.id, task.key)}</span>}
          {showTaskStatus && !currentWork && <Status state={state} />}
          {!reportSummary && !currentWork && <TaskWaitAge task={task} />}
          {owner && <Assignee task={task} compact />}
          {compactQueue && exceptionalPriority && (
            <span className="queue-task-priority"><TaskPriority priority={task.priority} /></span>
          )}
          {!currentWork && lastActivity}
        </div>
      </div>
      <div className="task-row-context">{context}</div>
      {report && !publicPreview && task.checklist_total > 0 ? (
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
  listRef, onClickCapture, searchQuery, publicPreview = false, projectNames }) {
  if (!items?.length) return null
  return (
    <ol ref={listRef} onClickCapture={onClickCapture} className={compactQueue ? 'task-rows compact-queue-rows' : 'task-rows'}
      aria-label={label} role="list">
      {items.map(task => (
        <TaskRow key={task.id} task={task} owner={owner} selected={task.id === selected || task.key === selected}
          report={report} ownReport={ownReport} revision={revision} compactQueue={compactQueue} searchQuery={searchQuery}
          publicPreview={publicPreview} projectLabel={projectNames?.get(task.key?.split('-')[0])} />
      ))}
    </ol>
  )
}
