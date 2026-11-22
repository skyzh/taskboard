import React, { useCallback, useState } from 'react'
import { queryPath, request, useLoadedPages, useResource } from './data.js'
import { ActivityDate } from './Overview.jsx'
import AttentionRequests from './AttentionRequests.jsx'
import DecisionForm, { mayAnswer } from './DecisionForm.jsx'
import { MergeQueue } from './MergeQueue.jsx'
import ReminderDate from './ReminderDate.jsx'
import Kanban from './Kanban.jsx'
import TaskClosure from './TaskClosure.jsx'
import TaskDetailsEdit from './TaskDetailsEdit.jsx'
import TaskOwnership from './TaskOwnership.jsx'
import TaskTags from './TaskTags.jsx'
import { TaskRows } from './WorkRow.jsx'
import {
  actions, ActivityMark, addFilterName, Assignee, AssigneeAvatar, ChecklistSteps, ClosureReference, Dependency, EvidenceLinks, LoadMore, Options, OutcomeNote, RecordedActivity, ReminderWhen, ResourceStatus, Status,
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
  layout, setLayout, phoneColumn, setPhoneColumn, kanbanNavigation }) {
  const kanban = layout === 'kanban'
  const closedAvailable = overview?.counts?.closed != null
  const { tag: selectedTag, ...otherFilters } = filters
  const tag = selectedTag?.project === project ? selectedTag?.name : undefined
  const taskFilters = { ...otherFilters, dri: filters.unassigned ? undefined : filters.dri, tag }
  const projectTags = useResource(project ? queryPath('/tags', { project }) : null, revision)
  const tagOptions = new Set(projectTags.data?.tags || [])
  if (tag) tagOptions.add(tag)
  const page = useLoadedPages(kanban ? null : '/tasks', {
    ...taskFilters, project, order: filters.state === 'ready' ? 'priority' : undefined,
  }, revision)
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
  const otherFilterCount = [ownerSelection, filters.waiting_on, tag].filter(Boolean).length
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
            <TaskRows items={page.data?.items} label="Work queue" compactQueue />
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
          <ClosureReference context={event.context} />
          {children}
          <OutcomeNote event={{ ...event, details }} disclosure="event-disclosure"
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

export function TaskDetail({ id, revision, session, onChanged, preferredProject = 'T' }) {
  const [checklistOpen, setChecklistOpen] = useState(true)
  const detail = useResource(`/tasks/${id}`, revision)
  const activity = useLoadedPages(`/tasks/${id}/activity`, {}, revision)
  const record = detail.data
  const task = record?.task
  const state = task && taskState(task)
  const summary = state === 'closed' ? 'Closure reason' : task?.summary_label
  const project = typeof id === 'string' ? id.split('-')[0] : 'T'
  const queueHref = project === 'T' && preferredProject === 'T' ? '#board' : `#project=${project}`
  return (
    <article id={taskHref(id).slice(1)} className="panel task-detail" aria-labelledby={`task-heading-${id}`} tabIndex={-1}>
      <a className="back-link" href={queueHref}>Back to Tasks</a>
      <p className="detail-meta">
        <span className="ticket-id">{taskLabel(id, task?.key)}</span>
        {record?.goal && <span>{record.goal.kind === 'standing' ? 'Standing goal' : 'Milestone'}</span>}
        {task && <><Status state={state} /><TaskPriority priority={task.priority} /></>}
        {task && <TaskWaitAge task={task} />}
        {task?.eta && state !== 'closed' && <span>Estimated completion <Timestamp at={task.eta} /></span>}
      </p>
      <h1 id={`task-heading-${id}`}><MarkdownInline text={task?.title || `Task ${taskLabel(id)}`} relatedTasks={record?.related_tasks} /></h1>
      <ResourceStatus resource={detail} label="task details" />
      {task && (
        <>
          <div className="detail-owner"><Assignee task={task} /><TaskWatch id={id} session={session} revision={revision} onChanged={onChanged} /></div>
          <TaskOwnership task={task} actions={record.ownership_actions} project={task.key?.split('-')[0] || project}
            revision={revision} onChanged={onChanged} />
          {record.can_edit_details && <TaskDetailsEdit key={task.id} task={task} brief={record.brief}
            titleLimit={record.goal?.kind === 'standing' ? 500 : undefined} onChanged={onChanged} />}
          {record.can_close && state !== 'closed' && state !== 'done' && (
            <TaskClosure key={task.id} task={task} onChanged={onChanged} />
          )}
          <TaskTags task={task} editable={record.tag_editable} onChanged={onChanged} />
          <RecordedActivity activity={record.last_activity} />
          {record.goal?.deadline && <p>Due <time dateTime={record.goal.deadline}>{record.goal.deadline}</time></p>}
          {record.goal?.tickets.length > 0 && (
            <ul className="context-list" aria-label="Work linked to this goal">
              {record.goal.tickets.map(ticket => <li key={ticket.id}>
                <TaskLink id={ticket.id} reference={ticket.key} title={ticket.title} />
              </li>)}
            </ul>
          )}
          {record.brief && <Markdown text={record.brief} className="detail-brief prose" relatedTasks={record.related_tasks} />}
          <section className={`ticket-focus ${state}`} aria-label={summary}>
            <h3>{summary}</h3>
            {task.waiting_on && <p><Dependency task={task} /></p>}
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
            {record.sources.length > 0 && (
              <details open><summary>Sources · {record.sources.length}</summary><EvidenceLinks links={record.sources} label="Sources" /></details>
            )}
          </section>
          {record.checklist.length > 0 && (
            <details className="detail-checklist" open={checklistOpen}
              onToggle={event => setChecklistOpen(event.currentTarget.open)}>
              <summary>
                <span>Checklist</span>
                <span>{task.checklist_completed} of {task.checklist_total} complete</span>
              </summary>
              <ChecklistSteps steps={record.checklist} relatedTasks={record.related_tasks} />
            </details>
          )}
        </>
      )}
      <DecisionForm key={`${id}:${session.data?.actor?.id ?? session.data?.actor?.name ?? ''}`}
        task={task} session={session} onChanged={onChanged} />
      {task && (
        <>
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
            <ResourceStatus resource={activity} label="task activity" />
            {activity.data?.items.length === 0 && <p className="section-message">No activity yet.</p>}
            <ol className="event-list" role="list">
              {activity.data?.items.map(event => <EventRow key={event.id} event={event} ticketActivity />)}
            </ol>
            <LoadMore page={activity} label={`activity for ${taskLabel(id, task.key)}`} />
          </section>
        </>
      )}
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
      <EvidenceLinks links={event.links} />
      <OutcomeNote event={event} headline={result} disclosure="event-disclosure"
        withEvidence={false} choiceShown waitShown={event.action === 'wait'} />
    </li>
  )
}

export function WorkLog({ requested, revision, ownerProfiles }) {
  const { formatDay } = useTimeZone()
  const [actor, setActor] = useState('')
  const [actorLabel, setActorLabel] = useState('')
  const [action, setAction] = useState('')
  const [complete, setComplete] = useState(false)
  const full = complete || Boolean(action)
  const filtered = Boolean(actorLabel || action || complete)
  const page = useLoadedPages('/activity', { scope: 'all', actor: actor.trim(), action, detail: full ? 'all' : 'outcomes' }, revision)
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
  return (
    <section className="work-log" id="work-log-title" aria-labelledby="activity" tabIndex={-1}>
      <div className="page-heading"><h1 id="activity">Activity</h1></div>
      <div className="filters log-filters">
        <label>
          Actor
          <input type="text" list="activity-actors" value={actorLabel} onChange={event => {
            const label = event.target.value
            setActorLabel(label)
            setActor(actorNames.get(label) || label.trim())
          }} />
          <datalist id="activity-actors">
            {[...actorNames.keys()].sort().map(name => <option key={name} value={name} />)}
          </datalist>
        </label>
        <label>
          Action
          <select value={action} onChange={event => setAction(event.target.value)}>
            <option value="">All actions</option>
            {Object.entries(actions).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <label className="activity-mode">
          <input type="checkbox" checked={complete} onChange={event => setComplete(event.target.checked)} />
          Include routine activity
        </label>
        {filtered && <button type="button" onClick={() => {
          setActor('')
          setActorLabel('')
          setAction('')
          setComplete(false)
        }}>Clear filters</button>}
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
