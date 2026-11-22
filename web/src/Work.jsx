import React, { useCallback, useEffect, useRef, useState } from 'react'
import { queryPath, request, useLoadedPages, useResource } from './data.js'
import { ActivityDate } from './Overview.jsx'
import AttentionRequests from './AttentionRequests.jsx'
import ChecklistEdit from './ChecklistEdit.jsx'
import DecisionForm, { mayAnswer } from './DecisionForm.jsx'
import { MergeQueue } from './MergeQueue.jsx'
import ReminderDate from './ReminderDate.jsx'
import Kanban from './Kanban.jsx'
import TaskActivityUpdate from './TaskActivityUpdate.jsx'
import TaskClosure from './TaskClosure.jsx'
import TaskDetailsEdit from './TaskDetailsEdit.jsx'
import TaskOwnership from './TaskOwnership.jsx'
import TaskTags from './TaskTags.jsx'
import { TaskRows } from './WorkRow.jsx'
import {
  actions, ActivityMark, addFilterName, Assignee, AssigneeAvatar, ChecklistSteps, ClosureReference, Dependency, EvidenceLinks, LoadMore, Options, OutcomeNote, PeerRequestContext, RecordedActivity, ReminderWhen, ResourceStatus, Status,
  states, TaskLink, TaskPriority, Timestamp, WorkerAssignment, WorkerRunState, WorkerRunTiming, eventHref, profileName,
  taskHref, taskLabel, taskState,
} from './shared.jsx'
import { activityHeadline, ticketActivityCopy } from './workCopy.js'
import { Markdown, MarkdownInline } from './Markdown.jsx'
import { TaskWaitAge } from './WaitAge.jsx'
import { useTimeZone } from './timeZone.js'
import { ReportTime } from './ReportTime.jsx'

const ownerOptionPrefix = 'owner:'

export function WorkQueue({ revision, overview, filters, setFilters, waitingLabel, setWaitingLabel, project, projectPicker,
  layout, setLayout, phoneColumn, setPhoneColumn, kanbanNavigation, listNavigation, listReturn }) {
  const kanban = layout === 'kanban'
  const closedAvailable = overview?.counts?.closed != null
  const { tag: selectedTag, ...otherFilters } = filters
  const tag = selectedTag?.project === project ? selectedTag?.name : undefined
  const taskFilters = { ...otherFilters, dri: filters.unassigned ? undefined : filters.dri, tag }
  const projectTags = useResource(project ? queryPath('/tags', { project }) : null, revision)
  const tagOptions = new Set(projectTags.data?.tags || [])
  if (tag) tagOptions.add(tag)
  const listFilters = {
    ...taskFilters, project, order: filters.state === 'ready' ? 'priority' : undefined,
  }
  const listScope = queryPath('/tasks', listFilters)
  const page = useLoadedPages(kanban ? null : '/tasks', listFilters, revision, { rememberedNavigation: listNavigation })
  const list = useRef(null)
  function rememberTicket(event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    const link = event.target.closest('.task-row-heading > a[href^="#task-"]')
    if (link) listReturn.current = {
      scope: listScope, board: window.location.hash || '#board', ticket: link.hash,
    }
  }
  useEffect(() => {
    const previous = listReturn?.current
    if (!previous) return
    if (kanban || previous.scope !== listScope || previous.board !== window.location.hash) {
      listReturn.current = null
      return
    }
    if (page.pending) return
    listReturn.current = null
    const link = [...(list.current?.querySelectorAll('.task-row-heading > a') || [])]
      .find(link => link.hash === previous.ticket)
    link?.focus({ preventScroll: true })
    link?.scrollIntoView({ block: 'center' })
  }, [kanban, listReturn, listScope, page.pending])
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
    ...Object.values(columnPages).filter(page => page?.project === project).flatMap(page => page.items),
  ]
  for (const task of visibleTasks) addFilterName(waitingOn, task.waiting_on, task.waiting_profile)
  const selectedOwnerProfile = visibleTasks.find(task => task.dri === filters.dri && task.dri_profile)?.dri_profile
  const otherFilterCount = [ownerSelection, filters.waiting_on, tag, filters.hide_requests].filter(Boolean).length
  const filterCount = otherFilterCount + Number(Boolean(filters.state))
  const stateCounts = otherFilterCount === 0 ? overview?.counts : undefined
  return (
    <div className="workspace">
      <section className="feed-section queue" aria-labelledby="queue-heading">
        <h2 id="queue-heading">Work queue</h2>
        <div className="queue-toolbar">
          {projectPicker && <div className="queue-project">{projectPicker}</div>}
          {setLayout && <div className="queue-layout" role="group" aria-label="Task view">
            <button type="button" aria-pressed={!kanban} onClick={() => setLayout('list')}>List</button>
            <button type="button" aria-pressed={kanban} onClick={() => setLayout('kanban')}>Kanban</button>
          </div>}
          <button type="button" className="queue-filter-toggle" aria-expanded={filtersOpen} aria-controls="queue-filters"
            data-active={filterCount > 0}
            onClick={() => setFiltersOpen(open => !open)}>
            {filterCount ? `Filters (${filterCount})` : 'Filters'}
          </button>
        </div>
        {overview?.merge_queue && <div className="queue-tools">
          <MergeQueue project={project} revision={revision} />
        </div>}
        <div className="filters" id="queue-filters" data-open={filtersOpen}>
          <label>
            Status
            <select value={filters.state || ''} onChange={event => update('state', event.target.value)}>
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
            Waiting on
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
        {kanban ? <Kanban key={project} project={project} filters={taskFilters} revision={revision}
          closedAvailable={closedAvailable} phoneColumn={phoneColumn} setPhoneColumn={setPhoneColumn}
          counts={stateCounts} navigation={kanbanNavigation} onPage={updateColumn} /> : (
          <>
            <ResourceStatus resource={page} label="the queue" />
            {page.data?.items.length === 0 && <p className="section-message" role="status">No matching tasks.</p>}
            <TaskRows items={page.data?.items} label="Work queue" compactQueue listRef={list} onClick={rememberTicket} />
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
  if (event.context?.wait_kind !== 'automation' &&
    headline.toLowerCase().startsWith('waiting on ' + owner.toLowerCase())) return null
  return <Dependency task={event} />
}

function ActivityChanges({ event }) {
  const context = event.context || {}
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

export function EventRow({ event, permalink = false, showTask = permalink, ticketActivity = false, children }) {
  const { formatTimestamp } = useTimeZone()
  const id = permalink ? eventHref(event.id).slice(1) : undefined
  const actor = profileName(event.actor, event.actor_profile)
  const result = activityHeadline(event)
  const timeLink = (
    <a href={eventHref(event.id)} aria-label={`Link to ${event.label.toLowerCase()} by ${actor} at ${formatTimestamp(event.at)}`}>
      <ReportTime at={event.at} linked />
    </a>
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
            <span className="assignee"><AssigneeAvatar name={actor} url={event.actor_profile?.avatar_url} />{actor}</span>
            <ActivityWait event={event} headline={result} />
            {timeLink}
          </p>
          {showTask && <p className="event-ticket"><TaskLink reference={event.task_key} id={event.task_id} title={event.task_title} /></p>}
          <ActivityChanges event={event} />
          <ClosureReference context={event.context} />
          {children}
          {firstEvidence && <EvidenceLinks links={[firstEvidence]} />}
          <OutcomeNote event={{ ...event, details, links: additionalEvidence }} disclosure="event-disclosure"
            choiceShown={Boolean(choice)} waitShown={event.action === 'wait'} />
        </div>
      </li>
    )
  }
  return (
    <li className="event-row" id={id} tabIndex={permalink ? -1 : undefined}>
      <p className="activity-result"><ActivityMark event={event} /> <MarkdownInline text={result} relatedTasks={event.related_tasks} /></p>
      <p className="event-meta">
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
      <p className="detail-meta">
        <Status state={state} />
        <TaskPriority priority={task.priority} />
        {task.eta && state !== 'closed' && <span>Estimated completion <Timestamp at={task.eta} /></span>}
      </p>
    </>
  )
}

function TicketContext({ record, id, task, state, wide, detailed, session, revision, onChanged }) {
  const [open, setOpen] = useState(false)
  const content = (
    <div className="ticket-context-content">
      {wide && <h2>Details</h2>}
      {wide ? <TicketIdentity task={task} state={state}>
        <TaskWatch id={id} session={session} revision={revision} onChanged={onChanged} />
      </TicketIdentity> : <TaskWatch id={id} session={session} revision={revision} onChanged={onChanged} />}
      <RecordedActivity activity={record.last_activity} />
      {record.goal?.deadline && <p>Due <time dateTime={record.goal.deadline}>{record.goal.deadline}</time></p>}
      {record.sources.length > 0 && (
        <details open><summary>Sources · {record.sources.length}</summary><EvidenceLinks links={record.sources} label="Sources" /></details>
      )}
      {!!record.related_tasks?.length && (
        <section aria-label="Related tasks">
          <h3>Related tasks</h3>
          <ul className="context-list">
            {record.related_tasks.map(item => (
              <li key={item.key}><a href={item.url}><span className="ticket-id">{item.key}</span> {item.title}</a></li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
  if (!detailed) return <div className="ticket-context">{content}</div>
  return (
    <details className="ticket-context" open={wide || open}
      onToggle={wide ? undefined : event => setOpen(event.currentTarget.open)}>
      <summary>Details</summary>
      {content}
    </details>
  )
}

export function TaskDetail({ id, revision, session, onChanged, preferredProject = 'T', returnHref, fromSearch }) {
  const [checklistOpen, setChecklistOpen] = useState(true)
  const article = useRef(null)
  const [wide, setWide] = useState(false)
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => setWide(entry.contentRect.width >= 960))
    observer.observe(article.current)
    return () => observer.disconnect()
  }, [])
  const detail = useResource(`/tasks/${id}`, revision)
  const activity = useLoadedPages(`/tasks/${id}/activity`, {}, revision)
  const record = detail.data
  const task = record?.task
  const state = task && taskState(task)
  const summary = state === 'closed' ? 'Closure reason' : task?.summary_label
  const project = typeof id === 'string' ? id.split('-')[0] : 'T'
  const queueHref = returnHref || (project === 'T' && preferredProject === 'T' ? '#board' : `#project=${project}`)
  const backHref = fromSearch ? `#${new URLSearchParams({ search: fromSearch })}` : queueHref
  const detailed = Boolean(record && (record.brief || record.checklist.length || record.sources.length
    || record.related_tasks?.length || record.goal?.tickets.length || state === 'human_review'))
  const sidebar = wide && detailed
  return (
    <article ref={article} id={taskHref(id).slice(1)} className={`panel task-detail${sidebar ? ' has-sidebar' : ''}`}
      aria-labelledby={`task-heading-${id}`} tabIndex={-1}>
      <a className="back-link" href={backHref}>{fromSearch ? 'Back to Search results' : 'Back to Tasks'}</a>
      <p className="detail-meta">
        <span className="ticket-id">{taskLabel(id, task?.key)}</span>
        {record?.goal && <span>{record.goal.kind === 'standing' ? 'Standing goal' : 'Milestone'}</span>}
      </p>
      <h1 id={`task-heading-${id}`}><MarkdownInline text={task?.title || `Task ${taskLabel(id)}`} relatedTasks={record?.related_tasks} /></h1>
      {task && !sidebar && <div className="ticket-primary-identity"><TicketIdentity task={task} state={state} /></div>}
      <ResourceStatus resource={detail} label="task details" />
      <div className="ticket-detail-layout">
        <div className="ticket-detail-work">
          <section className={task ? `ticket-focus ${state}` : undefined} aria-label={summary}>
            {task && <>
              <h2>{summary}</h2>
              <TaskWaitAge task={task} />
              {task.waiting_on && <p><Dependency task={task} /></p>}
              {task.peer_request && <PeerRequestContext task={task} />}
              <Markdown text={task.note} relatedTasks={record.related_tasks} />
              <ClosureReference context={task.context} />
              <ReminderWhen context={task.context} />
              {session.data?.actor?.id === task.dri && ['ready', 'in_progress', 'parked'].includes(task.state) && (
                <ReminderDate key={`${id}:${task.context?.defer_until || task.context?.review_after || ''}`}
                  path={`/tasks/${id}/reminder`} saved={task.context?.review_after}
                  savedUntil={task.context?.defer_until} exactTime onChanged={onChanged} />
              )}
              {!mayAnswer(task, session) && <Options options={task.context?.choices} />}
              <EvidenceLinks links={task.links} />
              {record.can_confirm_blocker && (
                <TaskActivityUpdate key={task.event_id} task={task} onChanged={onChanged} confirmBlocker />
              )}
            </>}
            <DecisionForm key={`${id}:${session.data?.actor?.id ?? session.data?.actor?.name ?? ''}`}
              task={task} session={session} onChanged={onChanged} />
          </section>
          {task && <>
            {record.goal?.tickets.length > 0 && (
              <ul className="context-list" aria-label="Work linked to this goal">
                {record.goal.tickets.map(ticket => <li key={ticket.id}>
                  <TaskLink id={ticket.id} reference={ticket.key} title={ticket.title} />
                </li>)}
              </ul>
            )}
            {record.brief && <Markdown text={record.brief} className="detail-brief prose" relatedTasks={record.related_tasks} />}
            {(record.checklist.length > 0 || record.can_edit_details) && (
              <details className="detail-checklist" open={checklistOpen}
                onToggle={event => setChecklistOpen(event.currentTarget.open)}>
                <summary>
                  <span>Checklist</span>
                  <span>{task.checklist_completed} of {task.checklist_total} complete</span>
                </summary>
                {record.checklist.length > 0 && <ChecklistSteps steps={record.checklist} relatedTasks={record.related_tasks}
                  showMissingTimes
                  editStep={record.can_edit_details ? step => <ChecklistEdit task={task} step={step} onChanged={onChanged} /> : undefined} />}
                {record.can_edit_details && <ChecklistEdit task={task} onChanged={onChanged} />}
              </details>
            )}
            <TaskOwnership task={task} actions={record.ownership_actions} project={task.key?.split('-')[0] || project}
              revision={revision} onChanged={onChanged} />
            {record.can_edit_details && <TaskDetailsEdit key={`details-${task.id}`} task={task} brief={record.brief}
              titleLimit={record.goal?.kind === 'standing' ? 500 : undefined} onChanged={onChanged} />}
            {record.can_close && state !== 'closed' && state !== 'done' && (
              <TaskClosure key={`close-${task.id}`} task={task} onChanged={onChanged} />
            )}
            <TaskTags task={task} editable={record.tag_editable} onChanged={onChanged} />
          </>}
        </div>
        {task && <TicketContext key={id} record={record} id={id} task={task} state={state} wide={sidebar} detailed={detailed}
          session={session} revision={revision} onChanged={onChanged} />}
        {task && (
          <div className="ticket-detail-history">
            {session.data?.actor?.kind === 'human' && (
              <details className="ticket-attention">
                <summary>Attention requests</summary>
                <AttentionRequests taskId={id} revision={revision} onChanged={onChanged} />
              </details>
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
            <section className="ticket-activity" aria-labelledby={`ticket-activity-${id}`}>
              <h3 id={`ticket-activity-${id}`}>Activity</h3>
              {['agent', 'human'].includes(session.data?.actor?.kind) && <TaskActivityUpdate task={task} onChanged={onChanged} />}
              <ResourceStatus resource={activity} label="task activity" />
              {activity.data?.items.length === 0 && <p className="section-message">No activity yet.</p>}
              <ol className="event-list" role="list">
                {activity.data?.items.map(event => <EventRow key={event.id} event={event} ticketActivity />)}
              </ol>
              <LoadMore page={activity} label={`activity for ${taskLabel(id, task.key)}`} />
            </section>
          </div>
        )}
      </div>
    </article>
  )
}

function ActivityEntry({ event, full, selected = false, exact = selected }) {
  const { formatTimestamp } = useTimeZone()
  const actor = profileName(event.actor, event.actor_profile)
  const result = activityHeadline(event)
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
            {full && <span>{event.label}</span>}
            <a href={eventHref(event.id)} aria-label={`Open activity by ${actor} at ${formatTimestamp(event.at)}`}>
              {exact ? <Timestamp at={event.at} /> : <ReportTime at={event.at} linked />}
            </a>
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

export function WorkLog({ requested, revision, ownerProfiles, fragment = 'activity', onFiltersChange }) {
  const { formatDay } = useTimeZone()
  const params = new URLSearchParams(fragment.split('?', 2)[1])
  const actor = params.get('actor')?.trim().slice(0, 160) || ''
  const requestedAction = params.get('action')
  const action = Object.hasOwn(actions, requestedAction) ? requestedAction : ''
  const complete = params.get('detail') === 'all'
  const hideRequests = params.get('hide_requests') === 'true'
  const [actorDraft, setActorDraft] = useState(null)
  const [filtersOpen, setFiltersOpen] = useState(false)
  const filterToggle = useRef(null)
  const actorInput = useRef(null)
  const full = complete || Boolean(action)
  const filterCount = [actor, action, complete, hideRequests].filter(Boolean).length
  const filtered = filterCount > 0
  const page = useLoadedPages('/activity', { scope: 'all', actor, action, detail: full ? 'all' : 'outcomes',
    ...(hideRequests && { hide_requests: true }) }, revision)
  const items = page.data?.items || []
  const onPage = items.some(event => event.id === requested)
  const linked = useResource(requested && !onPage ? `/activity/${requested}` : null, revision)
  const linkedEvent = linked.data?.event || linked.data
  const days = new Map()
  for (const event of items) {
    const day = formatDay(event.at)
    if (!days.has(day)) days.set(day, [])
    days.get(day).push(event)
  }
  const actorNames = new Map()
  for (const [key, profile] of Object.entries(ownerProfiles || {})) addFilterName(actorNames, key, profile)
  for (const event of items) addFilterName(actorNames, event.actor, event.actor_profile)
  if (linkedEvent) addFilterName(actorNames, linkedEvent.actor, linkedEvent.actor_profile)
  const actorLabel = actorDraft?.actor === actor ? actorDraft.label
    : ([...actorNames].find(([, key]) => key === actor)?.[0] || profileName(actor))

  function changeFilters(next, replace = false) {
    const filters = { actor, action, complete, hideRequests, ...next }
    const scope = new URLSearchParams({
      ...(filters.actor && { actor: filters.actor }),
      ...(filters.action && { action: filters.action }),
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
        <h1 id="activity">Activity</h1>
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
          Actor
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
      <ResourceStatus resource={page} label="activity" />
      {linked.pending && <p role="status" className="section-message">Loading linked activity…</p>}
      {linked.error && (
        <div id={eventHref(requested).slice(1)} tabIndex={-1}>
          <ResourceStatus resource={linked} label="linked activity" />
        </div>
      )}
      {linkedEvent && (
        <section className="outcome-day" aria-labelledby="linked-activity-heading">
          <h2 id="linked-activity-heading">Linked event</h2>
          <ol className="event-list" role="list">
            <ActivityEntry event={linkedEvent} full={full} exact />
          </ol>
        </section>
      )}
      {page.data?.items.length === 0 && !linked.pending && !linkedEvent && (
        <p className="section-message" role="status">{filtered ? 'No matching activity.' : 'No activity to show.'}</p>
      )}
      {[...days].sort(([left], [right]) => right.localeCompare(left)).map(([day, events]) => (
        <section className="outcome-day" aria-label={day} key={day}>
          <h2><ActivityDate day={day} /></h2>
          <ol className="event-list" role="list">
            {events.map(event => <ActivityEntry key={event.id} event={event} full={full} selected={event.id === requested} />)}
          </ol>
        </section>
      ))}
      <LoadMore page={page} label="activity" />
    </section>
  )
}
