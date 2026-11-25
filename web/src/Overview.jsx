import React, { useEffect, useState } from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { useLoadedPages } from './data.js'
import {
  ActivityMark, EvidenceLinks, LoadMore, OutcomeNote, ResourceStatus, TaskLink, Timestamp,
  eventHref, profileName, taskLabel,
} from './shared.jsx'
import { TaskRows } from './WorkRow.jsx'
import { activityHeadline, briefSourceText } from './workCopy.js'
import { ReportTime, useReportDay } from './ReportTime.jsx'
import { MarkdownInline } from './Markdown.jsx'
import { useTimeZone } from './timeZone.js'

const dayLabel = new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeZone: 'UTC' })
const shortDay = new Intl.DateTimeFormat('en-US', { month: 'numeric', day: 'numeric', timeZone: 'UTC' })
const overviewPages = { now: data => data.now, decisions: data => data.decisions }
const outcomeDaysPage = data => ({ items: data.days, next_cursor: data.next_day, week: data.week })

function ActivitySection({ id, title, field, overview, revision, empty, project, allProjects }) {
  const firstPage = overview.data?.[field]
  const page = useLoadedPages('/overview', allProjects ? { scope: 'all' } : { project }, revision, {
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

export function Overview({ resource, revision, project, allProjects }) {
  return (
    <>
      <ActivitySection
        id="now-title" title="Now" field="now" overview={resource} revision={revision} project={project} allProjects={allProjects}
        empty="No active tasks."
      />
      <ActivitySection
        id="decisions-title" title="Needs a decision" field="decisions" overview={resource}
        revision={revision} project={project} allProjects={allProjects} empty="No pending decisions."
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
          <BoardLink href={eventHref(latest.id)}><ReportTime at={latest.at} linked /></BoardLink>
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
                <BoardLink href={eventHref(event.id)}><Timestamp at={event.at} /></BoardLink>
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
          <BoardLink href={eventHref(latest.id)}><ReportTime at={latest.at} linked /></BoardLink>
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
                <BoardLink href={eventHref(event.id)}><Timestamp at={event.at} /></BoardLink>
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

function OutcomeDay({ day, owner, revision, project, allProjects, timeZone, empty = false }) {
  const page = useLoadedPages(empty ? null : '/outcomes', {
    dri: owner, day, ...(allProjects ? { scope: 'all' } : { project }), time_zone: timeZone,
  }, revision)
  const tasks = groupOutcomes(page.data?.items || [])
  const Row = owner ? OutcomeSummary : OutcomeRow
  return (
    <section className="outcome-day" aria-labelledby={`outcomes-${day}`}>
      <h3 id={`outcomes-${day}`} tabIndex={-1}><ActivityDate day={day} /></h3>
      {empty && <p className="section-message">No outcomes for this day.</p>}
      <ResourceStatus resource={page} label="outcomes" />
      <ol className="outcome-list" role="list">
        {[...tasks].map(([id, events]) => <Row key={id} events={events} />)}
      </ol>
      <LoadMore page={page} label={`outcomes for ${day}`} />
    </section>
  )
}

function OutcomeWeek({ week, openDay }) {
  return (
    <ol className="outcome-week" role="list" aria-label="Outcome counts for the last seven days">
      {week.map(({ day, count }) => (
        <li key={day}>
          <button type="button" data-level={Math.min(count, 3)} onClick={() => openDay(day)}
            aria-label={`${dayLabel.format(new Date(day))}, ${count} ${count === 1 ? 'outcome' : 'outcomes'}`}>
            <time dateTime={day}>{shortDay.format(new Date(day))}</time>
            <strong>{count}</strong>
          </button>
        </li>
      ))}
    </ol>
  )
}

export function Outcomes({ revision, owner, project, allProjects, showWeek = false }) {
  const { timeZone } = useTimeZone()
  const today = useReportDay()
  const [selection, setSelection] = useState(null)
  const page = useLoadedPages('/outcomes/days', {
    dri: owner, ...(allProjects ? { scope: 'all' } : { project }), time_zone: timeZone,
    ...(showWeek ? { week_ending: today } : {}),
  }, revision, {
    limit: 2, moreLimit: 1, cursorParam: 'before_day', readPage: outcomeDaysPage,
  })
  const days = page.data?.items || []
  const week = page.data?.week
  const selected = selection?.timeZone === timeZone && week?.find(item => item.day === selection.day)
  const selectedDay = selected?.day
  const displayedDays = selectedDay && !days.includes(selectedDay) ? [selectedDay, ...days] : days
  function openDay(day) {
    const heading = document.getElementById(`outcomes-${day}`)
    if (heading) heading.focus()
    else setSelection({ day, timeZone })
  }
  useEffect(() => {
    if (selectedDay) document.getElementById(`outcomes-${selectedDay}`)?.focus()
  }, [selectedDay])

  return (
    <section className="feed-section outcomes" aria-labelledby="outcomes-title">
      <h2 id="outcomes-title">{owner ? 'Recent outcomes' : 'Outcomes'}</h2>
      {week && <OutcomeWeek week={week} openDay={openDay} />}
      {!displayedDays.length && page.data && <p className="section-message">No outcomes yet.</p>}
      {displayedDays.map(day => (
        <OutcomeDay key={day} day={day} owner={owner} revision={revision} project={project} allProjects={allProjects}
          timeZone={timeZone} empty={day === selectedDay && selected.count === 0} />
      ))}
      <ResourceStatus resource={page} label="outcomes" />
      <LoadMore page={page} label="days of outcomes" />
    </section>
  )
}
