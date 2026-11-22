import React from 'react'
import { useLoadedPages } from './data.js'
import {
  ActivityMark, EvidenceLinks, LoadMore, OutcomeNote, ResourceStatus, TaskLink, Timestamp,
  eventHref, profileName, taskLabel,
} from './shared.jsx'
import { TaskRows } from './WorkRow.jsx'
import { activityHeadline, briefSourceText } from './workCopy.js'
import { ReportTime } from './ReportTime.jsx'
import { MarkdownInline } from './Markdown.jsx'
import { useTimeZone } from './timeZone.js'

const dayLabel = new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeZone: 'UTC' })
const overviewPages = { now: data => data.now, decisions: data => data.decisions }
const outcomeDaysPage = data => ({ items: data.days, next_cursor: data.next_day })

function ActivitySection({ id, title, field, overview, revision, empty, project }) {
  const firstPage = overview.data?.[field]
  const page = useLoadedPages('/overview', { project }, revision, {
    cursorParam: `${field}_before`, readPage: overviewPages[field], firstPage, enabled: Boolean(firstPage && !overview.pending),
  })
  const rows = page.data
  const label = field === 'now' ? 'active tasks' : 'tasks needing a decision'
  return (
    <section className="feed-section overview" aria-labelledby={id}>
      <h2 id={id}>{title}</h2>
      <ResourceStatus resource={overview.error ? overview : page} label={label} />
      <TaskRows items={rows?.items} label={title} />
      {rows?.items.length === 0 && <p className="section-message">{empty}</p>}
      <LoadMore page={page} label={label} />
    </section>
  )
}

export function Overview({ resource, revision, project }) {
  return (
    <>
      <ActivitySection
        id="now-title" title="Now" field="now" overview={resource} revision={revision} project={project}
        empty="No active tasks."
      />
      <ActivitySection
        id="decisions-title" title="Needs a decision" field="decisions" overview={resource}
        revision={revision} project={project} empty="No pending decisions."
      />
    </>
  )
}

export function ActivityDate({ day }) {
  return <time dateTime={day}>{dayLabel.format(new Date(day))}</time>
}

export function groupOutcomes(events) {
  const tasks = new Map()
  for (const event of events) {
    if (!tasks.has(event.task_id)) tasks.set(event.task_id, [])
    tasks.get(event.task_id).push(event)
  }
  return tasks
}

export function OutcomeSummary({ events }) {
  const latest = events[0]
  const choice = briefSourceText(latest.context?.choice)
  return (
    <li className="outcome-row" id={`event-${latest.id}-outcome`} tabIndex={-1}>
      <div className="outcome-compact">
        <ActivityMark event={latest} />
        <div>
          <div className="outcome-title">
            <TaskLink reference={latest.task_key} id={latest.task_id} title={latest.task_title} />
          </div>
          <p className="outcome-meta">{latest.label}{choice && <> · <strong><MarkdownInline text={choice} /></strong></>}</p>
        </div>
        <span className="outcome-meta outcome-time">
          {profileName(latest.actor, latest.actor_profile)}
          <a href={eventHref(latest.id)}><ReportTime at={latest.at} /></a>
        </span>
      </div>
      <details className="outcome-details">
        <summary aria-label={`Outcome and evidence for ${taskLabel(latest.task_id, latest.task_key)}`}>Outcome and evidence</summary>
        <ol className="expanded-outcomes" role="list">
          {events.map(event => (
            <li key={event.id}>
              <p className="event-meta">
                <strong>{event.label}</strong>
                <span>{profileName(event.actor, event.actor_profile)}</span>
                <a href={eventHref(event.id)}><Timestamp at={event.at} showDate={false} /></a>
              </p>
              <OutcomeNote event={event} choiceShown={event.id === latest.id && Boolean(choice)} />
            </li>
          ))}
        </ol>
      </details>
    </li>
  )
}

export function OutcomeRow({ events }) {
  const latest = events[0]
  const links = new Map()
  for (const event of events) {
    for (const link of event.links) links.set(JSON.stringify([link.label, link.url]), link)
  }
  const result = activityHeadline(latest)
  return (
    <li className="outcome-row" id={`event-${latest.id}-outcome`} tabIndex={-1}>
      <div className="outcome-compact">
        <ActivityMark event={latest} />
        <div>
          {(result || latest.context?.choice) && <p className="outcome-note">
            {latest.context?.choice && <strong><MarkdownInline text={latest.context.choice} />{result && ' · '}</strong>}
            <MarkdownInline text={result} relatedTasks={latest.related_tasks} />
          </p>}
          <div className="outcome-title">
            <TaskLink reference={latest.task_key} id={latest.task_id} title={latest.task_title} />
          </div>
          <EvidenceLinks links={[...links.values()]} />
        </div>
        <span className="outcome-meta outcome-time">
          {profileName(latest.actor, latest.actor_profile)}
          <a href={eventHref(latest.id)}><Timestamp at={latest.at} showDate={false} /></a>
        </span>
      </div>
      {events.length === 1 ? (
        <OutcomeNote event={latest} headline={result} disclosure="outcome-details" withEvidence={false} choiceShown />
      ) : <details className="outcome-details">
        <summary aria-label={`Full details for T-${latest.task_id}`}>Full details</summary>
        <ol className="expanded-outcomes" role="list">
          {events.map(event => (
            <li key={event.id}>
              <p className="event-meta">
                <strong>{event.label}</strong>
                <span>{profileName(event.actor, event.actor_profile)}</span>
                <a href={eventHref(event.id)}><Timestamp at={event.at} showDate={false} /></a>
              </p>
              <OutcomeNote event={event} headline={event.id === latest.id ? result : undefined}
                choiceShown={event.id === latest.id} withEvidence={false} />
            </li>
          ))}
        </ol>
      </details>}
    </li>
  )
}

function OutcomeDay({ day, owner, revision, project, timeZone }) {
  const page = useLoadedPages('/outcomes', { dri: owner, day, project, time_zone: timeZone }, revision)
  const tasks = groupOutcomes(page.data?.items || [])
  const Row = owner ? OutcomeSummary : OutcomeRow
  return (
    <section className="outcome-day" aria-labelledby={`outcomes-${day}`}>
      <h3 id={`outcomes-${day}`}><ActivityDate day={day} /></h3>
      <ResourceStatus resource={page} label="outcomes" />
      <ol className="outcome-list" role="list">
        {[...tasks].map(([id, events]) => <Row key={id} events={events} />)}
      </ol>
      <LoadMore page={page} label={`outcomes for ${day}`} />
    </section>
  )
}

export function Outcomes({ revision, owner, project }) {
  const { timeZone } = useTimeZone()
  const page = useLoadedPages('/outcomes/days', { dri: owner, project, time_zone: timeZone }, revision, {
    limit: 2, moreLimit: 1, cursorParam: 'before_day', readPage: outcomeDaysPage,
  })
  const days = page.data?.items || []
  return (
    <section className="feed-section outcomes" aria-labelledby="outcomes-title">
      <h2 id="outcomes-title">{owner ? 'Recent outcomes' : 'Outcomes'}</h2>
      {!days.length && page.data && <p className="section-message">No outcomes yet.</p>}
      {days.map(day => <OutcomeDay key={day} day={day} owner={owner} revision={revision} project={project} timeZone={timeZone} />)}
      <ResourceStatus resource={page} label="outcomes" />
      <LoadMore page={page} label="days of outcomes" />
    </section>
  )
}
