import TicketAttachments from './TicketAttachments.jsx'
import React, { useCallback, useEffect, useRef, useState } from 'react'
import { BoardLink, useContentTitle } from './BoardNavigation.jsx'
import { ListFilter } from 'lucide-react'
import { isBackendError } from './boardAvailability.js'
import { queryPath, request, useLoadedPages, useResource } from './data.js'
import { ActivityDate } from './Overview.jsx'
import { entryHref, StagedWork, TicketKnowledgeLinks } from './Knowledge.jsx'
import AttentionRequests from './AttentionRequests.jsx'
import ChecklistEdit from './ChecklistEdit.jsx'
import DecisionForm, { LinkedDecision, mayAnswer } from './DecisionForm.jsx'
import DecisionReceiptActions, { decisionsHref } from './DecisionReceiptActions.jsx'
import EditToggle from './EditToggle.jsx'
import { useFieldGuidance } from './FieldGuidance.jsx'
import { MergeQueue } from './MergeQueue.jsx'
import ParkingForm from './ParkingForm.jsx'
import RecordHistory, { historyHref } from './RecordHistory.jsx'
import ReminderDate from './ReminderDate.jsx'
import { GoalReviewTime } from './Schedules.jsx'
import RecurringRuns, { RecurringRunList } from './RecurringRuns.jsx'
import Kanban from './Kanban.jsx'
import NeedsYouSend from './NeedsYouSend.jsx'
import { NotificationLogEntry } from './NotificationLog.jsx'
import PeerAskSend from './PeerAskSend.jsx'
import TaskActivityUpdate from './TaskActivityUpdate.jsx'
import TaskClosure from './TaskClosure.jsx'
import TaskDetailsEdit from './TaskDetailsEdit.jsx'
import TaskOwnership from './TaskOwnership.jsx'
import TaskProjectMove from './TaskProjectMove.jsx'
import TaskTags from './TaskTags.jsx'
import TaskTransition from './TaskTransition.jsx'
import TicketFields from './TicketFields.jsx'
import TicketRecurrence from './TicketRecurrence.jsx'
import { TaskRows } from './WorkRow.jsx'
import {
  actions, ActivityMark, addFilterName, Assignee, AssigneeAvatar, ChecklistSteps, ClosureReference, Dependency, EvidenceLinks, LoadMore, Options, OutcomeNote, PeerRequestContext, RecordedActivity, ResourceStatus, Status,
  states, TaskLink, TaskPriority, Timestamp, WorkerAssignment, WorkerRunState, WorkerRunTiming, eventHref, isCheckpointActivity, isDecisionActivity, profileName,
  taskHref, taskLabel, taskState,
} from './shared.jsx'
import { activityHeadline, ticketActivityCopy, waitLabel } from './workCopy.js'
import { Markdown, MarkdownInline } from './Markdown.jsx'
import { TaskWaitAge } from './WaitAge.jsx'
import { useTimeZone } from './timeZone.js'
import { ReportTime } from './ReportTime.jsx'
import { GoalDeadline } from './DriGoal.jsx'
import SegmentedControl from './SegmentedControl.jsx'
import { projectFilters } from './projectScope.js'
import { groupWatcherActivity } from './watcherActivity.js'

const ownerOptionPrefix = 'owner:'
const layoutOptions = [{ value: 'list', label: 'List' }, { value: 'kanban', label: 'Kanban' }]

export function WorkQueue({ revision, overview, filters, setFilters, waitingLabel, setWaitingLabel, project, projectPicker, allProjects,
  layout, setLayout, phoneColumn, setPhoneColumn, kanbanNavigation, listNavigation, listReturn, fragment = 'board' }) {
  const kanban = layout === 'kanban'
  const closedAvailable = overview?.counts?.closed != null
  const { tag: selectedTag, ...otherFilters } = filters
  const tag = !allProjects && selectedTag?.project === project ? selectedTag?.name : undefined
  const taskFilters = { ...otherFilters, dri: filters.unassigned ? undefined : filters.dri, tag }
  const projectTags = useResource(project && !allProjects ? queryPath('/tags', { project }) : null, revision)
  const tagOptions = new Set(projectTags.data?.tags || [])
  if (tag) tagOptions.add(tag)
  const listFilters = {
    ...taskFilters, ...(allProjects ? { scope: 'all' } : { project }), order: filters.state === 'ready' ? 'priority' : undefined,
  }
  const listScope = queryPath('/tasks', listFilters)
  const page = useLoadedPages(kanban ? null : '/tasks', listFilters, revision, { rememberedNavigation: listNavigation })
  const list = useRef(null)
  function rememberTicket(event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    const link = event.target.closest('.task-row-heading > a[data-ticket-fragment]')
    if (link) listReturn.current = { scope: listScope, board: '#' + fragment, ticket: link.dataset.ticketFragment }
  }
  useEffect(() => {
    const previous = listReturn?.current
    if (!previous) return
    if (kanban || previous.scope !== listScope || previous.board !== '#' + fragment) {
      listReturn.current = null
      return
    }
    if (page.pending) return
    listReturn.current = null
    const link = [...(list.current?.querySelectorAll('.task-row-heading > a') || [])]
      .find(link => link.dataset.ticketFragment === previous.ticket)
    link?.focus({ preventScroll: true })
    link?.scrollIntoView({ block: 'center' })
  }, [fragment, kanban, listReturn, listScope, page.pending])
  const [columnPages, setColumnPages] = useState({})
  const [filtersOpen, setFiltersOpen] = useState(false)
  const updateColumn = useCallback((state, data) => {
    setColumnPages(previous => ({ ...previous, [state]: data }))
  }, [])
  const update = (name, value) => setFilters(previous => ({ ...previous, [name]: value }))
  const ownerSelection = filters.unassigned ? 'unassigned' : filters.dri ? ownerOptionPrefix + filters.dri : ''
  function updateOwner(event) {
    const value = event.target.value
    setFilters(previous => ({
      ...previous,
      dri: value.startsWith(ownerOptionPrefix) ? value.slice(ownerOptionPrefix.length) : undefined,
      unassigned: value === 'unassigned' ? true : undefined,
    }))
  }
  const owners = new Set([...(overview?.owners || []), filters.dri].filter(Boolean))
  const waitingOn = new Map()
  for (const [key, profile] of Object.entries(overview?.owner_profiles || {})) addFilterName(waitingOn, key, profile)
  const visibleTasks = [
    ...(overview?.decisions?.items || []),
    ...(overview?.now?.items || []),
    ...(page.data?.items || []),
    ...Object.values(columnPages).filter(page => page?.project === (allProjects ? undefined : project)).flatMap(page => page.items),
  ]
  for (const task of visibleTasks) addFilterName(waitingOn, task.waiting_on, task.waiting_profile)
  const selectedOwnerProfile = visibleTasks.find(task => task.dri === filters.dri && task.dri_profile)?.dri_profile
  const otherFilterCount = [ownerSelection, filters.waiting_on, tag, filters.hide_requests].filter(Boolean).length
  const groupedStatus = filters.current_work ? 'Current work' : filters.unfinished ? 'Unfinished' : ''
  const filterCount = otherFilterCount + Number(Boolean(filters.state || groupedStatus))
  const collapsedStatus = filtersOpen ? '' : groupedStatus || states[filters.state]
  const otherFiltersLabel = otherFilterCount > 0
    ? `, ${otherFilterCount} more active ${otherFilterCount === 1 ? 'filter' : 'filters'}` : ''
  const stateCounts = otherFilterCount === 0 ? overview?.counts : undefined
  return (
    <div className="workspace">
      <section className="feed-section queue" aria-labelledby="queue-heading">
        <h2 id="queue-heading">Work queue</h2>
        <div className="queue-toolbar">
          {projectPicker && <div className="queue-project">{projectPicker}</div>}
          {setLayout && <SegmentedControl className="queue-layout" label="Task view"
            value={kanban ? 'kanban' : 'list'} options={layoutOptions} onChange={setLayout} />}
          <button type="button" className="queue-filter-toggle" aria-expanded={filtersOpen} aria-controls="queue-filters"
            data-active={filterCount > 0} data-status={Boolean(collapsedStatus)}
            aria-label={collapsedStatus ? `Filters, ${collapsedStatus}${otherFiltersLabel}` : undefined}
            onClick={() => setFiltersOpen(open => !open)}>
            {collapsedStatus ? (
              <>
                <ListFilter size="1em" aria-hidden="true" />
                <span>{collapsedStatus}</span>
                {otherFilterCount > 0 && <span className="queue-filter-extra" aria-hidden="true">{`+${otherFilterCount}`}</span>}
              </>
            ) : (filterCount ? `Filters (${filterCount})` : 'Filters')}
          </button>
        </div>
        {!allProjects && overview?.merge_queue && <div className="queue-tools">
          <MergeQueue project={project} revision={revision} />
        </div>}
        <div className="filters" id="queue-filters" data-open={filtersOpen}>
          <label>
            Status
            <select value={groupedStatus ? 'linked-work' : filters.state || ''} onChange={event => {
              const state = event.target.value
              setFilters(previous => ({ ...previous, state, current_work: undefined, unfinished: undefined, watching: undefined }))
            }}>
              {groupedStatus && <option value="linked-work" disabled>{groupedStatus}</option>}
              <option value="">All statuses</option>
              {Object.entries(states).filter(([key]) => key !== 'closed' || closedAvailable).map(([key, label]) => (
                <option key={key} value={key}>{label}{stateCounts?.[key] != null ? ` (${stateCounts[key]})` : ''}</option>
              ))}
            </select>
          </label>
          <label>
            Owner
            <select value={ownerSelection} onChange={updateOwner}>
              <option value="">All owners</option>
              <option value="unassigned">Unassigned</option>
              {[...owners].map(owner => (
                <option key={owner} value={ownerOptionPrefix + owner}>
                  {profileName(owner, overview?.owner_profiles?.[owner] ?? (filters.dri === owner ? selectedOwnerProfile : undefined))}
                </option>
              ))}
            </select>
          </label>
          <label>
            Wait or blocker
            <input
              type="text" list="dependencies" value={waitingLabel}
              onChange={event => {
                const label = event.target.value
                setWaitingLabel(label)
                update('waiting_on', waitingOn.get(label) || label.trim())
              }}
            />
            <datalist id="dependencies">
              {[...waitingOn.keys()].sort().map(name => <option key={name} value={name} />)}
            </datalist>
          </label>
          <label className="activity-mode">
            <input type="checkbox" checked={Boolean(filters.hide_requests)}
              onChange={event => update('hide_requests', event.target.checked || undefined)} />
            Hide peer requests
          </label>
          {tagOptions.size > 0 && (
            <label>
              Tag
              <select value={tag || ''} onChange={event => {
                const name = event.target.value
                update('tag', name ? { project, name } : undefined)
              }}>
                <option value="">All tags</option>
                {[...tagOptions].sort().map(name => <option key={name} value={name}>{name}</option>)}
              </select>
            </label>
          )}
        </div>
        {kanban ? <Kanban key={allProjects ? 'all' : project} project={allProjects ? undefined : project} allProjects={allProjects}
          filters={taskFilters} revision={revision}
          closedAvailable={closedAvailable} phoneColumn={phoneColumn} setPhoneColumn={setPhoneColumn}
          counts={stateCounts} navigation={kanbanNavigation} onPage={updateColumn} /> : (
          <>
            <ResourceStatus resource={page} label="the queue" />
            {page.data?.items.length === 0 && <p className="section-message" role="status">No matching tasks.</p>}
            <TaskRows items={page.data?.items} label="Work queue" compactQueue listRef={list} onClickCapture={rememberTicket} />
            <LoadMore page={page} label="tasks in the work queue" />
          </>
        )}
      </section>
    </div>
  )
}

function ActivityWait({ event, headline }) {
  if (event.action !== 'wait' || !event.waiting_on) return null
  const owner = profileName(event.waiting_on, event.waiting_profile)
  if (headline.toLowerCase().startsWith(`${waitLabel(event)} ${owner}`.toLowerCase())) return null
  return <Dependency task={event} />
}

function ActivityChanges({ event }) {
  const context = event.context || {}
  if (context.transition_override?.reason) {
    return (
      <details className="event-disclosure">
        <summary>Override reason</summary>
        <p>{context.transition_override.reason}</p>
      </details>
    )
  }
  if (context.previous_dri !== undefined && context.next_dri !== undefined && context.previous_dri !== context.next_dri) {
    const previous = context.previous_dri ? profileName(context.previous_dri, event.previous_dri_profile) : 'Unassigned'
    const current = context.next_dri ? profileName(context.next_dri, event.next_dri_profile) : 'Unassigned'
    return <p className="event-meta">From {previous} to {current}</p>
  }
  const audit = context.audit
  if (!audit) return null
  if (audit.action === 'edit-task-details') {
    const fields = ['title', 'brief'].filter(field => Object.hasOwn(audit.current || {}, field))
    if (!fields.length) return null
    return (
      <details className="event-disclosure">
        <summary>What changed</summary>
        <dl className="activity-changes">
          {fields.map(field => (
            <React.Fragment key={field}>
              <dt>Previous {field}</dt><dd>{audit.previous?.[field] || 'Empty'}</dd>
              <dt>Updated {field}</dt><dd>{audit.current[field] || 'Empty'}</dd>
            </React.Fragment>
          ))}
        </dl>
      </details>
    )
  }
  if (audit.action?.startsWith('checklist-')) {
    return (
      <p className="event-meta">
        {audit.previous_label && <span>Previous wording {audit.previous_label}</span>}
        <span>{audit.label || 'Step text was not recorded.'}</span>
      </p>
    )
  }
  if (['reference-add', 'reference-relabel', 'source-attach'].includes(audit.action) && audit.label) {
    return <p className="event-meta">{audit.label}</p>
  }
  return null
}

export function EventRow({ event, permalink = false, showTask = permalink, ticketActivity = false, progression = false, children }) {
  const { formatTimestamp } = useTimeZone()
  const id = permalink ? eventHref(event.id).slice(1) : undefined
  const actor = profileName(event.actor, event.actor_profile)
  const result = activityHeadline(event)
  const activityType = isDecisionActivity(event) ? actions.decision : isCheckpointActivity(event) ? actions.checkpoint
    : event.goal_review_completed ? 'Goal review' : null
  const timeLink = (
    <BoardLink href={eventHref(event.id)} aria-label={`Link to ${event.label.toLowerCase()} by ${actor} at ${formatTimestamp(event.at)}`}>
      <ReportTime at={event.at} linked />
    </BoardLink>
  )
  if (ticketActivity) {
    const { choice, result: visibleResult, details } = ticketActivityCopy(event, result)
    const [firstEvidence, ...additionalEvidence] = event.links || []
    return (
      <li className="event-row ticket-event-row" id={id} tabIndex={permalink ? -1 : undefined}>
        <ActivityMark event={event} />
        <div className="ticket-event-content">
          <p className="activity-result">
            {choice && <><MarkdownInline text={choice} relatedTasks={event.related_tasks} />{visibleResult && ' · '}</>}
            {visibleResult && <MarkdownInline text={visibleResult} relatedTasks={event.related_tasks} />}
          </p>
          <p className="event-meta">
            {activityType && <span>{activityType}</span>}
            <span className="assignee"><AssigneeAvatar name={actor} url={event.actor_profile?.avatar_url} />{actor}</span>
            <ActivityWait event={event} headline={result} />
            {timeLink}
          </p>
          {showTask && <p className="event-ticket"><TaskLink reference={event.task_key} id={event.task_id} title={event.task_title} /></p>}
          <ActivityChanges event={event} />
          <ClosureReference context={event.context} />
          <div className="ticket-event-actions">
            {firstEvidence && <EvidenceLinks links={[firstEvidence]} />}
            <OutcomeNote event={{ ...event, details, links: additionalEvidence }} disclosure={progression ? undefined : "event-disclosure"}
              disclosureLabel="Activity details" choiceShown={Boolean(choice)} waitShown={event.action === 'wait'} />
          </div>
          {children}
        </div>
      </li>
    )
  }
  return (
    <li className="event-row" id={id} tabIndex={permalink ? -1 : undefined}>
      <p className="activity-result"><ActivityMark event={event} /> <MarkdownInline text={result} relatedTasks={event.related_tasks} /></p>
      <p className="event-meta">
        {activityType && <span>{activityType}</span>}
        <span>{actor}</span>
        <ActivityWait event={event} headline={result} />
        {timeLink}
      </p>
      {showTask && <p className="event-ticket"><TaskLink reference={event.task_key} id={event.task_id} title={event.task_title} /></p>}
      <ActivityChanges event={event} />
      <ClosureReference context={event.context} />
      {children}
      <OutcomeNote event={event} headline={result} disclosure="event-disclosure"
        withEvidence={false} waitShown={event.action === 'wait'} />
      <EvidenceLinks links={event.links} />
    </li>
  )
}

function TaskWatch({ id, session, revision, onChanged }) {
  const verified = ['agent', 'human'].includes(session.data?.actor?.kind)
  const state = useResource(verified ? `/tasks/${id}/watch` : null, revision)
  const [saving, setSaving] = useState(false)
  const [failure, setFailure] = useState(null)
  if (!verified) return null

  async function toggle() {
    setSaving(true)
    setFailure(null)
    try {
      await request(`/tasks/${id}/watch`, { body: { watch: !state.data.watching } })
      onChanged()
    } catch (error) {
      setFailure(error.message)
    } finally {
      setSaving(false)
    }
  }
  return (
    <div className="task-watch">
      <ResourceStatus resource={state} label="watch status" />
      {state.data && <button type="button" disabled={saving || state.pending} aria-pressed={state.data.watching} onClick={toggle}>
        {state.data.watching ? 'Stop watching' : 'Watch task'}
      </button>}
      {failure && <p role="alert">{failure}</p>}
    </div>
  )
}

function TicketIdentity({ task, state, children }) {
  return (
    <>
      <div className="detail-owner"><Assignee task={task} />{children}</div>
      <p className="detail-meta ticket-status-meta">
        <Status state={state} />
        <TaskPriority priority={task.priority} />
      </p>
    </>
  )
}

function ticketProperties({ task, goal, recurrence, recurring_runs: recurringRuns }, state, editingTags) {
  const { defer_until: deferUntil, review_after: reviewDate, readiness } = task.context || {}
  const nextRun = goal?.review_schedule?.next_at ?? (recurrence ? recurrence.next_at : recurringRuns?.next_at)
  const reviewTime = goal?.review_schedule?.review_time ?? goal?.review_time
  return [
    deferUntil ? ['Defer until', <Timestamp at={deferUntil} />] :
      reviewDate && ['Review date', <time dateTime={reviewDate}>{reviewDate}</time>],
    state === 'parked' && readiness?.source_key && ['Review after',
      <TaskLink id={readiness.source_id} reference={readiness.source_key} />],
    nextRun && ['Next run', <Timestamp at={nextRun} />],
    reviewTime && ['Daily review', <><time dateTime={reviewTime}>{reviewTime}</time> Pacific
      {goal?.review_schedule?.state === 'paused' && ' · Paused'}</>],
    task.eta && state !== 'closed' && ['Estimated completion', <Timestamp at={task.eta} />],
    goal?.deadline && ['Due', <GoalDeadline deadline={goal.deadline} finished={state === 'done' || state === 'closed'} />],
    !editingTags && task.tags?.length && ['Tags', task.tags.join(', ')],
  ].filter(Boolean)
}

function TicketReferences({ record }) {
  return <>
    {record.sources?.length > 0 && (
      <details open><summary>Sources · {record.sources.length}</summary><EvidenceLinks links={record.sources} label="Sources" ticketSources /></details>
    )}
    {!!record.related_tasks?.length && (
      <section aria-label="Related tasks">
        <h3>Related tasks</h3>
        <ul className="context-list">
          {record.related_tasks.map(item => (
            <li key={item.key}><BoardLink href={item.url}><span className="ticket-id">{item.key}</span> {item.title}</BoardLink></li>
          ))}
        </ul>
      </section>
    )}
  </>
}

function TicketContext({ record, id, task, state, wide, detailed, properties, session, revision, onChanged, showActions }) {
  const [open, setOpen] = useState(false)
  const watch = <div hidden={!showActions}><TaskWatch id={id} session={session} revision={revision} onChanged={onChanged} /></div>
  const content = (
    <div className="ticket-context-content">
      {wide && <h2>Ticket details</h2>}
      {wide ? <TicketIdentity task={task} state={state}>
        {watch}
      </TicketIdentity> : watch}
      {(properties.length > 0 || record.ticket_fields?.fields?.length > 0) && (
        <dl className="ticket-properties">
          {properties.map(([label, value]) => (
            <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
          ))}
          <TicketFields configuration={record.ticket_fields} ticket={task.key || id}
            editing={showActions} onChanged={onChanged} />
        </dl>
      )}
      <RecordedActivity activity={record.last_activity} />
      <TicketReferences record={record} />
    </div>
  )
  if (!detailed) return <div className="ticket-context">{content}</div>
  return (
    <details className="ticket-context" open={wide || open}
      onToggle={wide ? undefined : event => setOpen(event.currentTarget.open)}>
      <summary>Ticket details</summary>
      {content}
    </details>
  )
}

function RecordActivity({ id, task, activity, knowledge, children }) {
  const events = activity.data?.items || []
  const recurringRuns = activity.data?.recurrence_runs?.filter(run =>
    run.task_id !== task.id || run.state === 'missed' || run.state === 'skipped')
  const groups = knowledge ? events.map(event => [event]) : groupWatcherActivity(events)
  return (
    <section className="ticket-activity" aria-labelledby={`ticket-activity-${id}`}>
      <h3 id={`ticket-activity-${id}`}>Activity</h3>
      {children}
      <ResourceStatus resource={activity} label={knowledge ? 'Knowledge activity' : 'task activity'} />
      {activity.data?.items.length === 0 && !recurringRuns?.length && <p className="section-message">No activity yet.</p>}
      <ol className="event-list" role="list">
        {groups.map(([latest, ...earlier]) => (
          <EventRow key={latest.id} event={latest} ticketActivity>
            {earlier.length > 0 && (
              <details className="event-disclosure watcher-event-group">
                <summary>{earlier.length + 1} matching entries</summary>
                <ol className="event-list" role="list">
                  {earlier.map(event => <EventRow key={event.id} event={event} ticketActivity />)}
                </ol>
              </details>
            )}
          </EventRow>
        ))}
      </ol>
      <LoadMore page={activity} label={`activity for ${taskLabel(id, task.key)}`} />
      {recurringRuns?.length > 0 && (
        <details className="recurring-runs">
          <summary>Other scheduled runs</summary>
          <RecurringRunList runs={recurringRuns} currentTaskId={task.id} showOutcome />
        </details>
      )}
    </section>
  )
}

function TicketProgression({ id, record, activity }) {
  const timelineHeading = useRef(null)
  const events = (activity.data?.items || []).filter(event =>
    isCheckpointActivity(event) || isDecisionActivity(event) || event.goal_review_completed)
    .sort((first, second) => Date.parse(first.at) - Date.parse(second.at) || first.id - second.id)
  return (
    <div className="ticket-progression">
      <button type="button" className="progression-jump" onClick={() => {
        timelineHeading.current.focus({ preventScroll: true })
        timelineHeading.current.scrollIntoView({ block: 'start' })
      }}>Jump to checkpoints</button>
      <section className="detail-brief" aria-labelledby={`task-description-${id}`}>
        <h2 id={`task-description-${id}`}>Description</h2>
        {record.brief ? <Markdown text={record.brief} className="prose" relatedTasks={record.related_tasks} />
          : <p className="section-message">No description yet.</p>}
      </section>
      <section className="ticket-activity" aria-labelledby={`ticket-progression-${id}`}>
        <h2 id={`ticket-progression-${id}`} ref={timelineHeading} tabIndex={-1}>Progression</h2>
        <ResourceStatus resource={activity} label="progression" />
        {activity.data && !events.length && !activity.pending && !activity.error && (
          <p className="section-message">{activity.data.next_cursor == null
            ? 'No progression recorded yet.' : 'No progression in the loaded activity.'}</p>
        )}
        <ol className="event-list" role="list">
          {events.map(event => <EventRow key={event.id} event={event} ticketActivity progression />)}
        </ol>
        <LoadMore page={activity} label={`older activity for ${taskLabel(id, record.task.key)}`} />
      </section>
    </div>
  )
}

function KnowledgeRecordActions({ record, id, activity, signedIn, onChanged }) {
  const [editing, setEditing] = useState(false)
  return <>
    {signedIn && record.can_edit_details && <>
      <EditToggle label="Edit Knowledge entry" active={editing} onToggle={() => setEditing(value => !value)} />
      <div hidden={!editing}>
        <TaskDetailsEdit key={id} task={record.task} brief={record.brief} onChanged={onChanged} />
        <TaskTags task={record.task} editable={editing && record.tag_editable} canTagDecisionRecord={false}
          onChanged={onChanged} />
      </div>
    </>}
    <RecordActivity id={id} task={record.task} activity={activity} knowledge />
  </>
}

export function KnowledgeRecordTools({ id, revision, onChanged }) {
  const path = `/tasks/${encodeURIComponent(id)}`
  const detail = useResource(path, revision)
  const record = detail.data
  const knowledge = record?.task?.record_kind === 'knowledge'
  const activity = useLoadedPages(knowledge ? `${path}/activity` : null, {}, revision)
  if (!record) return <ResourceStatus resource={detail} label="record details" />
  if (!knowledge) return null
  return <KnowledgeRecordActions record={record} id={id} activity={activity} signedIn onChanged={onChanged} />
}

export function TaskDetail({ id, revision, session, onChanged, onMoved, moveReceipt,
  preferredProject = 'T', returnHref, fromSearch, history, progression, decisionProject, attentionRequest, decisionRequest, preview }) {
  const [checklistOpen, setChecklistOpen] = useState(true)
  const [actionsOpen, setActionsOpen] = useState(false)
  useEffect(() => {
    if (history) setActionsOpen(false)
  }, [history])
  const article = useRef(null)
  const [wide, setWide] = useState(false)
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => setWide(entry.contentRect.width >= 960))
    observer.observe(article.current)
    return () => observer.disconnect()
  }, [])
  const detail = useResource(`/tasks/${id}`, revision)
  const activity = useLoadedPages(history ? null : `/tasks/${id}/activity`, progression ? { view: 'progression' } : {}, revision)
  const record = detail.data
  const task = record?.task
  useContentTitle(task?.title ? `${taskLabel(id, task.key)} · ${task.title}` : null)
  const knowledge = task?.record_kind === 'knowledge'
  const showProgression = progression && task && !knowledge
  const state = task && !knowledge && taskState(task)
  const summary = state === 'closed' ? 'Closure reason' : task?.summary_label
  const project = typeof id === 'string' ? id.split('-')[0] : 'T'
  const taskProject = task?.key?.split('-')[0] || project
  const signedIn = ['agent', 'human'].includes(session.data?.actor?.kind)
  const showActions = signedIn && actionsOpen
  const editingTags = showActions && record?.tag_editable
  const canEditChecklist = record?.can_edit_checklist ?? record?.tag_editable
  const canEditChecklistText = record?.can_edit_checklist_text ?? canEditChecklist
  const canAnswer = task && mayAnswer(task, session)
  const returnToDecisions = <BoardLink className="decision-return" href={decisionsHref(decisionProject)}>Return to decisions</BoardLink>
  const ownsTicket = task && signedIn && session.data.actor.id && session.data.actor.id === task.dri
  const ownsReturnPath = ownsTicket
    && ['ready', 'in_progress', 'parked'].includes(task.state)
  const showReturnPath = !record?.goal && typeof record?.can_edit_parking === 'boolean'
    ? signedIn && record.can_edit_parking : ownsReturnPath
  const { data: fieldGuidance } = useFieldGuidance(signedIn && task && !knowledge ? taskProject : null, revision)
  const fields = fieldGuidance?.fields
  const queueHref = returnHref || (taskProject === 'T' && preferredProject === 'T' ? '#board' : `#project=${taskProject}`)
  const backHref = fromSearch ? `#${new URLSearchParams({ search: fromSearch })}`
    : knowledge ? entryHref(task.key) : queueHref
  const backLabel = fromSearch ? 'Back to Search results' : knowledge ? 'Open in Knowledge' : 'Back to Tasks'
  const properties = task && !knowledge ? ticketProperties(record, state, editingTags) : []
  const detailed = Boolean(record && !knowledge && (record.brief || record.checklist.length || record.sources.length
    || record.related_tasks?.length || record.goal?.tickets.length || state === 'human_review' || properties.length
    || record.ticket_fields?.fields?.length || editingTags))
  const sidebar = wide && detailed && !showProgression
  const anchor = taskHref(id)
  const context = new URLSearchParams()
  if (fromSearch) context.set('from-search', fromSearch)
  if (decisionProject) context.set('decision-scope', decisionProject)
  if (decisionRequest !== undefined) context.set('decision-request', decisionRequest ?? '')
  if (attentionRequest) context.set('attention-request', attentionRequest)
  const currentHref = context.size ? `${anchor}?${context}` : anchor
  context.set('view', 'progression')
  const progressionHref = `${anchor}?${context}`
  if (history && task) {
    return (
      <article ref={article} id={anchor.slice(1)} className="panel task-detail" aria-label="History" tabIndex={-1}>
        <RecordHistory record={taskLabel(id, task.key)} currentHref={currentHref} history={history} revision={revision}
          knowledge={task.record_kind === 'knowledge'} />
      </article>
    )
  }
  return (
    <article ref={article} id={anchor.slice(1)} className={`panel task-detail${sidebar ? ' has-sidebar' : ''}`}
      aria-labelledby={`task-heading-${id}`} tabIndex={-1}>
      <div className="ticket-heading-meta">
        <BoardLink className="back-link" href={backHref}>{backLabel}</BoardLink>
        <p className="detail-meta">
          <span className="ticket-id">{taskLabel(id, task?.key)}</span>
          {knowledge && <span>Knowledge</span>}
          {record?.goal && <span>{record.goal.kind === 'standing' ? 'Standing goal' : 'Milestone'}</span>}
        </p>
        {task && !knowledge && (showProgression
          ? <BoardLink className="history-link" href={currentHref}>Ticket overview</BoardLink>
          : <BoardLink className="history-link" href={progressionHref}>Progression</BoardLink>)}
        {task && <BoardLink className="history-link" href={historyHref(currentHref)}>History</BoardLink>}
        {signedIn && task && !knowledge && !showProgression && <EditToggle label="Edit ticket" active={showActions}
          onToggle={() => setActionsOpen(open => !open)} />}
      </div>
      <h1 id={`task-heading-${id}`}><MarkdownInline text={task?.title || `Task ${taskLabel(id)}`} relatedTasks={record?.related_tasks} /></h1>
      {moveReceipt && <p className="section-message" role="status">{moveReceipt}</p>}
      {task && !knowledge && !sidebar && <div className="ticket-primary-identity"><TicketIdentity task={task} state={state} /></div>}
      <ResourceStatus resource={detail} label="task details" />
      {showProgression ? <TicketProgression id={id} record={record} activity={activity} /> : knowledge ? (
        <div className="knowledge-record-content">
          {record.brief && <Markdown text={record.brief} className="prose detail-brief" relatedTasks={record.related_tasks} />}
          {Boolean(record.sources?.length || record.related_tasks?.length) &&
            <div className="ticket-context-content"><TicketReferences record={record} /></div>}
          <StagedWork changes={record.knowledge_stages?.filter(change => change.entry === task.key)} />
          {signedIn && <TicketKnowledgeLinks ticket={task.key} revision={revision} />}
          <KnowledgeRecordActions record={record} id={id} activity={activity} signedIn={signedIn} onChanged={onChanged} />
        </div>
      ) : <div className="ticket-detail-layout">
        <div className="ticket-detail-work">
          {task && attentionRequest && session.data?.actor?.kind === 'human' && (
            <AttentionRequests key={`attention:${session.data.actor.id}:${id}:${attentionRequest}`}
              taskId={id} requestId={attentionRequest} revision={revision} onChanged={onChanged} />
          )}
          {decisionRequest !== undefined ? <section className="ticket-focus" aria-label="Decision request">
            <div className="ticket-focus-heading"><h2>Decision request</h2>{returnToDecisions}</div>
            {decisionRequest === null ? <p role="status">This decision request is unavailable.</p>
              : <LinkedDecision key={`${id}:${decisionRequest}:${session.data?.actor?.id || ''}`}
              ticket={id} requestId={decisionRequest} task={task} session={session} revision={revision}
              onChanged={onChanged} receiptAction={<DecisionReceiptActions project={decisionProject} revision={revision} showReturn={false} />} />}
          </section> : <section className={task ? `ticket-focus ${state}` : undefined} aria-label={summary}>
            {task && <>
              {canAnswer ? <div className="ticket-focus-heading">
                <h2>{summary}</h2>
                {returnToDecisions}
              </div> : <h2>{summary}</h2>}
              <TaskWaitAge task={task} />
              {(task.waiting_on || task.blockers) && <p><Dependency task={task} /></p>}
              {task.peer_request && <PeerRequestContext task={task} />}
              <Markdown text={task.note} relatedTasks={record.related_tasks} />
              {record.goal?.parked_note && <>
                <h3>Owner’s note</h3>
                <Markdown text={record.goal.parked_note} relatedTasks={record.related_tasks} />
              </>}
              {record.goal?.resume_review_time && <p>Resume at the next {record.goal.resume_review_time} Pacific goal review.</p>}
              <ClosureReference context={task.context} />
              {showReturnPath && (!record.goal?.keep_exploring || task.state === 'parked') && <div hidden={!showActions}>{record.goal ? (
                  <ReminderDate key={task.event_id}
                    path={`/tasks/${id}/reminder`} saved={task.context?.review_after}
                    savedUntil={task.context?.defer_until} exactTime
                    recurring={Boolean(record.goal.review_schedule)}
                    onChanged={onChanged} />
                ) : <ParkingForm key={task.event_id} task={task} onChanged={onChanged} />}</div>}
              {!canAnswer && <Options options={task.context?.choices} />}
              <EvidenceLinks links={task.links} />
              {record.can_confirm_blocker && (
                <div hidden={!showActions}>
                  <TaskActivityUpdate key={task.event_id} task={task} onChanged={onChanged} field={fields?.activity} confirmBlocker />
                </div>
              )}
            </>}
            <DecisionForm key={`${id}:${session.data?.actor?.id ?? session.data?.actor?.name ?? ''}`}
              task={task} session={session} onChanged={onChanged}
              receiptAction={canAnswer ? undefined : <DecisionReceiptActions key={decisionProject || 'all'}
                project={decisionProject} revision={revision} />} />
          </section>}
          {task && <>
            {signedIn && <NeedsYouSend key={`${id}:${session.data.actor.id}`} ticket={task.key || id}
              actions={record.needs_you_actions} project={taskProject} revision={revision} onChanged={onChanged} />}
            {signedIn && <PeerAskSend key={`peer:${id}:${session.data.actor.id}`} ticket={task.key || id}
              allowed={record.can_ask_agent === true} actor={session.data.actor.id}
              project={taskProject} revision={revision} onChanged={onChanged} />}
            {record.goal?.tickets.length > 0 && (
              <ul className="context-list" aria-label="Work linked to this goal">
                {record.goal.tickets.map(ticket => <li key={ticket.id}>
                  <TaskLink id={ticket.id} reference={ticket.key} title={ticket.title} />
                </li>)}
              </ul>
            )}
            {record.brief && <section className="detail-brief" aria-labelledby={`task-description-${id}`}>
              <h2 id={`task-description-${id}`}>Description</h2>
              <Markdown text={record.brief} className="prose" relatedTasks={record.related_tasks} />
            </section>}
            {signedIn && !preview && <TicketAttachments key={`${session.data.actor.id}:${task.key || id}`}
              ticket={task.key || id} revision={revision} onChanged={onChanged} />}
            {signedIn && <TicketKnowledgeLinks ticket={task.key || id} revision={revision} />}
            {(record.checklist.length > 0 || canEditChecklistText) && (
              <details className="detail-checklist" open={checklistOpen}
                hidden={!record.checklist.length && !showActions}
                onToggle={event => setChecklistOpen(event.currentTarget.open)}>
                <summary>
                  <span>Checklist</span>
                  <span>{task.checklist_completed} of {task.checklist_total} complete</span>
                </summary>
                {record.checklist.length > 0 && <ChecklistSteps steps={record.checklist} relatedTasks={record.related_tasks}
                  showMissingTimes
                  editStep={canEditChecklistText ? step => <div hidden={!showActions}>
                    <ChecklistEdit task={task} step={step} canManageStep={canEditChecklist} onChanged={onChanged} />
                  </div> : undefined} />}
                {canEditChecklistText && <div hidden={!showActions}><ChecklistEdit task={task} onChanged={onChanged} /></div>}
              </details>
            )}
            <div hidden={!showActions}>
              <TaskTransition key={task.event_id} task={task} actions={record.transition_actions}
                ownsTicket={ownsTicket} onChanged={onChanged} />
              <TaskOwnership key={task.event_id} task={task} actions={record.ownership_actions} project={taskProject}
                revision={revision} canAssignOnReopen={record.can_assign_on_reopen} onChanged={onChanged} />
              {signedIn && record.ownership_block_reason && <p className="section-message">{record.ownership_block_reason}</p>}
              {signedIn && record.move_destinations?.length > 0 &&
                <TaskProjectMove key={task.key} task={task} destinations={record.move_destinations} onMoved={onMoved} />}
              {signedIn && record.move_block_reason && <p className="section-message">{record.move_block_reason}</p>}
              {record.can_edit_details && <TaskDetailsEdit key={`details-${task.id}`} task={task} brief={record.brief}
                onChanged={onChanged} fields={fields} />}
              {record.can_close && state !== 'closed' && state !== 'done' && (
                <TaskClosure key={`close-${task.id}`} task={task} onChanged={onChanged} />
              )}
            </div>
            <TaskTags task={task} editable={editingTags} canTagDecisionRecord={record.can_tag_decision_record ?? record.tag_editable}
              onChanged={onChanged} />
            {record.goal?.schedule_editable
              ? <GoalReviewTime key={`goal-review:${task.key || id}`} goal={{ ...record.goal, ticket: task.key || id }}
                onChanged={onChanged} />
              : <TicketRecurrence key={`recurrence:${task.key || id}`} ticket={task.key || id} schedule={record.recurrence}
                canEnable={record.can_enable_recurrence === true} signedIn={signedIn} onChanged={onChanged} />}
          </>}
        </div>
        {task && <TicketContext key={id} record={record} id={id} task={task} state={state} wide={sidebar} detailed={detailed} properties={properties}
          session={session} revision={revision} onChanged={onChanged} showActions={showActions} />}
        {task && (
          <div className="ticket-detail-history">
            {!attentionRequest && session.data?.actor?.kind === 'human' && (
              <AttentionRequests key={`attention:${session.data.actor.id}:${id}`}
                taskId={id} revision={revision} onChanged={onChanged} showControls={showActions} />
            )}
            {record.workers.length > 0 && (
              <section className="detail-workers" aria-label="Recorded worker runs">
                <h3>Recorded worker runs</h3>
                <ul className="context-list">
                  {record.workers.map(worker => (
                    <li key={worker.id}>
                      W-{worker.id} · {profileName(worker.worker, worker.worker_profile)} · <WorkerRunState run={worker} />
                      <WorkerRunTiming run={worker} />
                      {(worker.effort?.trim() || worker.brief?.trim()) && (
                        <details>
                          <summary>Assignment</summary>
                          <WorkerAssignment run={worker} />
                        </details>
                      )}
                      {worker.report && <Markdown text={worker.report} />}
                      {worker.disposition && <Markdown text={worker.disposition} />}
                    </li>
                  ))}
                </ul>
              </section>
            )}
            <RecordActivity id={id} task={task} activity={activity}>
              {!record.recurrence && activity.data?.recurrence_runs == null && <RecurringRuns key={id} history={record.recurring_runs} />}
              {signedIn && <div hidden={!showActions}><TaskActivityUpdate task={task} onChanged={onChanged} field={fields?.activity} />
                {record.can_edit_details && <TaskActivityUpdate task={task} onChanged={onChanged} decision />}
              </div>}
            </RecordActivity>
          </div>
        )}
      </div>}
    </article>
  )
}

export function ActivityEntry({ event, full, selected = false, exact = selected }) {
  const { formatTimestamp } = useTimeZone()
  const actor = profileName(event.actor, event.actor_profile)
  const result = activityHeadline(event)
  const label = isDecisionActivity(event) ? actions.decision : isCheckpointActivity(event) ? actions.checkpoint : full && event.label
  return (
    <li className="event-row activity-entry with-mark" id={eventHref(event.id).slice(1)}
      tabIndex={-1} aria-current={selected ? 'location' : undefined}>
      <div className="activity-entry-main">
        <ActivityMark event={event} />
        <div>
          {(result || event.context?.choice) && <p className="activity-result event-summary">
            {event.context?.choice && <strong><MarkdownInline text={event.context.choice} />{result && ' · '}</strong>}
            <MarkdownInline text={result} relatedTasks={event.related_tasks} />
          </p>}
          <div className="activity-entry-line event-meta">
            <TaskLink reference={event.task_key} id={event.task_id} title={event.task_title} />
            <span>{actor}</span>
            {selected && <span>Linked event</span>}
            <ActivityWait event={event} headline={result} />
            {label && <span>{label}</span>}
            <BoardLink href={eventHref(event.id)} aria-label={`Open activity by ${actor} at ${formatTimestamp(event.at)}`}>
              {exact ? <Timestamp at={event.at} /> : <ReportTime at={event.at} linked />}
            </BoardLink>
          </div>
        </div>
      </div>
      <ActivityChanges event={event} />
      <EvidenceLinks links={event.links} />
      <OutcomeNote event={event} headline={result} disclosure="event-disclosure"
        withEvidence={false} choiceShown waitShown={event.action === 'wait'} />
    </li>
  )
}

export function WorkLog({ requested, revision, ownerProfiles, fragment = 'activity', project, projectPicker, onFiltersChange }) {
  const { formatDay } = useTimeZone()
  const params = new URLSearchParams(fragment.split('?', 2)[1])
  const actor = params.get('actor')?.trim().slice(0, 160) || ''
  const requestedAction = params.get('action')
  const wakes = requestedAction === 'wake'
  const pendingWakes = wakes && params.get('wake_status') === 'pending'
  const action = wakes || Object.hasOwn(actions, requestedAction) ? requestedAction : ''
  const complete = params.get('detail') === 'all'
  const hideRequests = params.get('hide_requests') === 'true'
  const [actorDraft, setActorDraft] = useState(null)
  const [filtersOpen, setFiltersOpen] = useState(false)
  const filterToggle = useRef(null)
  const actorInput = useRef(null)
  const full = complete || Boolean(action)
  const filterCount = [actor, action, complete, hideRequests].filter(Boolean).length
  const filtered = filterCount > 0
  const page = useLoadedPages('/activity', { ...projectFilters(project), actor, action, detail: full ? 'all' : 'outcomes',
    ...(hideRequests && { hide_requests: true }), ...(pendingWakes && { wake_status: 'pending' }) }, revision)
  const items = page.data?.items || []
  const onPage = !wakes && items.some(event => event.id === requested)
  const linked = useResource(requested && !onPage ? `/activity/${requested}` : null, revision)
  const linkedEvent = linked.data?.event || linked.data
  const openEvent = requested && ((!wakes && items.find(event => event.id === requested)) || linkedEvent)
  useContentTitle(openEvent ? activityHeadline(openEvent) || openEvent.label : null)
  const showLinkedErrorAnchor = linked.error && !linkedEvent && !isBackendError(linked.error)
  const days = new Map()
  for (const event of items) {
    const day = formatDay(wakes ? event.source_at : event.at)
    if (!days.has(day)) days.set(day, [])
    days.get(day).push(event)
  }
  const actorNames = new Map()
  for (const [key, profile] of Object.entries(ownerProfiles || {})) addFilterName(actorNames, key, profile)
  if (!wakes) for (const event of items) addFilterName(actorNames, event.actor, event.actor_profile)
  if (linkedEvent) addFilterName(actorNames, linkedEvent.actor, linkedEvent.actor_profile)
  const actorLabel = actorDraft?.actor === actor ? actorDraft.label
    : ([...actorNames].find(([, key]) => key === actor)?.[0] || profileName(actor))

  function changeFilters(next, replace = false) {
    const filters = { actor, action, complete, hideRequests, pendingWakes, ...next }
    const scope = new URLSearchParams({
      ...(params.has('project') || params.get('scope') === 'all' ? projectFilters(project) : {}),
      ...(filters.actor && { actor: filters.actor }),
      ...(filters.action && { action: filters.action }),
      ...(filters.action === 'wake' && filters.pendingWakes && { wake_status: 'pending' }),
      ...(filters.complete && { detail: 'all' }),
      ...(filters.hideRequests && { hide_requests: 'true' }),
    })
    const base = requested ? eventHref(requested).slice(1) : 'activity'
    onFiltersChange(scope.size ? `${base}?${scope}` : base, replace)
  }

  function clearFilters() {
    setActorDraft(null)
    changeFilters({ actor: '', action: '', complete: false, hideRequests: false })
    const target = filterToggle.current?.getClientRects().length ? filterToggle.current : actorInput.current
    target?.focus()
  }
  return (
    <section className="work-log" id="work-log-title" aria-labelledby="activity" tabIndex={-1}>
      <div className="page-heading activity-heading">
        <h1 id="activity">{pendingWakes ? 'Pending notifications' : 'Activity'}</h1>
        {projectPicker}
        <div className="activity-filter-controls">
          <button type="button" className="activity-filter-toggle" ref={filterToggle}
            aria-expanded={filtersOpen} aria-controls="activity-filters" data-active={filtered}
            onClick={() => setFiltersOpen(open => !open)}>
            {filtered ? `Filters (${filterCount})` : 'Filters'}
          </button>
          {filtered && <button type="button" className="activity-filter-clear" onClick={clearFilters}>Clear filters</button>}
        </div>
      </div>
      <div className="filters log-filters" id="activity-filters" data-open={filtersOpen}>
        <label>
          {wakes ? 'Agent' : 'Actor'}
          <input type="text" list="activity-actors" ref={actorInput} maxLength={160} value={actorLabel} onChange={event => {
            const label = event.target.value
            const selectedActor = actorNames.get(label) || label.trim()
            setActorDraft({ actor: selectedActor, label })
            changeFilters({ actor: selectedActor }, true)
          }} />
          <datalist id="activity-actors">
            {[...actorNames.keys()].sort().map(name => <option key={name} value={name} />)}
          </datalist>
        </label>
        <label>
          Action
          <select value={action} onChange={event => changeFilters({ action: event.target.value })}>
            <option value="">All actions</option>
            <option value="wake">Wake</option>
            {Object.entries(actions).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <label className="activity-mode">
          <input type="checkbox" checked={complete} onChange={event => changeFilters({ complete: event.target.checked })} />
          Include routine activity
        </label>
        <label className="activity-mode">
          <input type="checkbox" checked={hideRequests} onChange={event => changeFilters({ hideRequests: event.target.checked })} />
          Hide peer requests
        </label>
      </div>
      {wakes && <>
        <button type="button" onClick={() => changeFilters({ pendingWakes: !pendingWakes })}>
          {pendingWakes ? 'Show all wake activity' : 'Show pending notifications'}
        </button>
        <p>{pendingWakes
          ? 'Current Ready work and the wake evidence recorded so far.'
          : 'Ready events are possible wake triggers. Each stage shows the evidence recorded for that event.'}</p>
      </>}
      <ResourceStatus resource={page} label="activity" />
      {showLinkedErrorAnchor
        ? <div id={eventHref(requested).slice(1)} tabIndex={-1}>
          <ResourceStatus resource={linked} label="linked activity" />
        </div>
        : <ResourceStatus resource={linked} label="linked activity" />}
      {linkedEvent && (
        <section className="outcome-day" aria-labelledby="linked-activity-heading">
          <h2 id="linked-activity-heading">Linked event</h2>
          <ol className="event-list" role="list">
            <ActivityEntry event={linkedEvent} full={full} exact />
          </ol>
        </section>
      )}
      {page.data?.items.length === 0 && !linked.pending && !linkedEvent && (
        <p className="section-message" role="status">{pendingWakes ? 'No pending notifications for these filters.'
          : filtered ? 'No matching activity.' : 'No activity to show.'}</p>
      )}
      {[...days].sort(([left], [right]) => right.localeCompare(left)).map(([day, events]) => (
        <section className="outcome-day" aria-label={day} key={day}>
          <h2><ActivityDate day={day} /></h2>
          <ol className="event-list" role="list">
            {events.map(event => wakes ? <NotificationLogEntry key={event.id} item={event} />
              : <ActivityEntry key={event.id} event={event} full={full} selected={event.id === requested} />)}
          </ol>
        </section>
      ))}
      <LoadMore page={page} label="activity" />
    </section>
  )
}
