import React from 'react'
import { DemoUnavailable } from '../../src/DemoControl.jsx'
import { Markdown } from '../../src/Markdown.jsx'
import { OccurrenceHistory, ScheduleSummary } from '../../src/Schedules.jsx'
import { AppearanceSettings } from '../../src/Settings.jsx'
import { AssigneeAvatar, Options, TaskLink, Timestamp } from '../../src/shared.jsx'
import { EventRow } from '../../src/Work.jsx'
import { TaskRows } from '../../src/WorkRow.jsx'
import {
  approval, approvalEvent, askEvent, capturedAt, decision, decisionEvent, events, knowledge, knowledgeHref,
  personalAgentsGuide, profiles, project, repository, review, scheduleEvent, tasks, weeklyRun,
} from './fixture.js'
import { EventLink } from './DemoWork.jsx'
import '../../src/stats.css'

function Heading({ children, actions }) {
  return <div className="page-heading"><h1 id="demo-page-title" tabIndex={-1}>{children}</h1>{actions}</div>
}

export function KnowledgePage({ entry, query }) {
  const selected = [knowledge.index, knowledge.note].find(record => record.key === entry)
  const matching = [knowledge.index, knowledge.note].filter(record =>
    `${record.title} ${record.brief}`.toLowerCase().includes(query.toLowerCase()))
  const showIndex = selected === knowledge.index
  return (
    <section className="knowledge-page" aria-labelledby="demo-page-title">
      <Heading actions={<DemoUnavailable>New Knowledge entry</DemoUnavailable>}>Knowledge</Heading>
      <form className="demo-search" role="search" aria-label="Search Knowledge" key={query} onSubmit={event => {
        event.preventDefault()
        const value = new FormData(event.currentTarget).get('q').trim()
        window.location.hash = value ? 'knowledge?' + new URLSearchParams({ q: value }) : 'knowledge'
      }}>
        <label>Search Knowledge <input name="q" type="search" defaultValue={query} /></label>
        <button type="submit">Search</button>
      </form>
      {selected ? <article className="panel" aria-labelledby="demo-knowledge-entry">
        <a className="back-link" href={showIndex ? '#knowledge' : knowledgeHref(knowledge.index.key)}>
          {showIndex ? 'All areas' : 'Back to area index'}
        </a>
        <h2 id="demo-knowledge-entry" tabIndex={-1}>{selected.title}</h2>
        <Markdown text={selected.brief} />
        {showIndex ? <p><a href={knowledgeHref(knowledge.note.key)}>{knowledge.note.title}</a></p> : <>
          <h3>Sources</h3>
          <ul className="context-list">
            <li><TaskLink id={decision.id} reference={decision.key} title={decision.title} /></li>
            <li><EventLink event={decisionEvent}>Recorded decision</EventLink></li>
          </ul>
          <h3>Staged work</h3>
          <p><TaskLink id={review.id} reference={review.key} title={review.title} /></p>
          <div className="demo-actions">
            <DemoUnavailable>Edit title and brief</DemoUnavailable>
            <DemoUnavailable>History</DemoUnavailable>
            <DemoUnavailable>Stage work</DemoUnavailable>
            <DemoUnavailable>Apply staged work</DemoUnavailable>
          </div>
          <h3>Activity</h3>
          <ol className="event-list" role="list"><EventRow event={decisionEvent} ticketActivity /></ol>
        </>}
      </article> : <section className="panel" aria-label={query ? 'Knowledge results' : 'Areas'}>
        <h2>{query ? 'Search results' : 'Areas'}</h2>
        {query ? matching.length ? <ul className="context-list">{matching.map(record =>
          <li key={record.key}><a href={knowledgeHref(record.key)}>{record.title}</a></li>)}</ul>
          : <p role="status">No matching entries.</p>
          : <p><a href={knowledgeHref(knowledge.index.key)}>{knowledge.area}</a></p>}
      </section>}
    </section>
  )
}

const statsPeriods = { hour: 'Hour', day: 'Day', week: 'Week' }
const hoursByPeriod = { hour: 1, day: 24, week: 168 }

export function StatsPage({ period }) {
  const since = Date.parse(capturedAt) - hoursByPeriod[period] * 3_600_000
  const recorded = events.filter(event => Date.parse(event.at) >= since)
  const changedTasks = tasks.filter(task => recorded.some(event => event.task_id === task.id))
  return (
    <section className="stats-page" aria-labelledby="demo-page-title">
      <Heading actions={<nav className="demo-links" aria-label="Stats period">
        {Object.entries(statsPeriods).map(([key, label]) =>
          <a key={key} href={'#stats?period=' + key} aria-current={key === period ? 'page' : undefined}>{label}</a>)}
      </nav>}>Stats</Heading>
      <section className="panel" aria-labelledby="demo-recorded-work">
        <h2 id="demo-recorded-work">Recorded work</h2>
        <p className="stats-note"><Timestamp at={new Date(since).toISOString()} /> to <Timestamp at={capturedAt} /></p>
        <dl className="stats-totals">
          <div><dt>Recorded events</dt><dd>{recorded.length}</dd></div>
          <div><dt>Tickets with activity</dt><dd>{changedTasks.length}</dd></div>
          <div><dt>Decisions</dt><dd>{recorded.filter(event => event.action === 'decide').length}</dd></div>
        </dl>
      </section>
      <section className="panel" aria-labelledby="demo-stats-tickets">
        <h2 id="demo-stats-tickets">Tickets with activity</h2>
        <TaskRows items={changedTasks} label="Tickets with recorded activity" />
      </section>
    </section>
  )
}

const inboxTabs = [
  ['notifications', 'Inbox'], ['notifications-needs-me', 'Needs me'], ['notifications-log', 'Notification log'],
]

export function InboxPage({ tab }) {
  return (
    <section className="notifications" aria-labelledby="demo-page-title">
      <Heading>Notifications</Heading>
      <nav className="demo-links" aria-label="Notifications views">{inboxTabs.map(([id, label]) =>
        <a key={id} href={'#' + id} aria-current={tab === id ? 'page' : undefined}>{label}</a>)}</nav>
      <section className="panel" aria-labelledby="demo-inbox-content">
        <h2 id="demo-inbox-content">{inboxTabs.find(([id]) => id === tab)[1]}</h2>
        {tab === 'notifications-log' ? <>
          <p>Juniper received a wake notice for the scheduled inventory check.</p>
          <ul className="event-list" role="list"><EventRow event={scheduleEvent} showTask /></ul>
        </> : <>
          <ol className="event-list" role="list">
            <EventRow event={approvalEvent} showTask />
            {tab === 'notifications' && <EventRow event={askEvent} showTask />}
          </ol>
          <Options options={approval.context.choices} />
          <div className="demo-actions">
            <DemoUnavailable>Answer decision</DemoUnavailable>
            <DemoUnavailable>Dismiss notification</DemoUnavailable>
          </div>
        </>}
      </section>
    </section>
  )
}

const schedule = {
  title: weeklyRun.title, owner_name: profiles.get(weeklyRun.dri).name, cadence_hours: 168,
  state: 'active', next_at: '2026-09-23T17:30:00Z',
  recent_occurrences: [{ due_at: scheduleEvent.at, status: 'pending', task_id: weeklyRun.id, task_key: weeklyRun.key }],
}

export function SchedulesPage() {
  return <>
    <Heading actions={<a href="#board">Back to Tasks</a>}>Schedules</Heading>
    <section className="panel schedule-panel" aria-labelledby="demo-recurring-title">
      <h2 id="demo-recurring-title">Recurring work</h2>
      <article className="schedule-item">
        <h3>{schedule.title}</h3>
        <ScheduleSummary schedule={schedule} />
        <OccurrenceHistory occurrences={schedule.recent_occurrences} />
        <div className="demo-actions">
          <DemoUnavailable>Pause schedule</DemoUnavailable>
          <DemoUnavailable>Edit schedule</DemoUnavailable>
        </div>
      </article>
    </section>
  </>
}

export function SettingsPage({ theme, timeZonePreference, chooseTimeZone }) {
  return (
    <section className="settings" aria-labelledby="demo-page-title">
      <Heading>Settings</Heading>
      <AppearanceSettings theme={theme} timeZonePreference={timeZonePreference} chooseTimeZone={chooseTimeZone} />
      <section className="panel settings-panel" aria-labelledby="demo-profile-heading">
        <h2 id="demo-profile-heading">Profile</h2>
        <p className="assignee"><AssigneeAvatar name="Avery" />Avery · {profiles.get('avery').role}</p>
        <DemoUnavailable>Edit profile</DemoUnavailable>
      </section>
    </section>
  )
}

export function IntegrationsPage() {
  return (
    <section className="settings integrations" aria-labelledby="demo-page-title">
      <Heading>Integrations</Heading>
      <section className="panel settings-panel" aria-labelledby="demo-personal-agents">
        <h2 id="demo-personal-agents">Personal Agents</h2>
        <p>Connect the coding agents you already use so they can find their tickets and record results in Taskboard.</p>
        <p><a href={personalAgentsGuide}>Read the Personal Agents guide</a></p>
        <DemoUnavailable>Authorize a CLI connection</DemoUnavailable>
      </section>
    </section>
  )
}

export function AboutPage() {
  return (
    <section className="about" aria-labelledby="demo-page-title">
      <Heading>About</Heading>
      <div className="panel about-panel">
        <p>This public walkthrough uses a fictional {project.title} project.</p>
        <p><a href={repository}>View Taskboard on GitHub</a></p>
      </div>
    </section>
  )
}

export function OverviewPage() {
  return (
    <section aria-labelledby="demo-page-title">
      <Heading actions={<a href="#reports">Back to Reports</a>}>Team overview</Heading>
      <section className="panel" aria-labelledby="demo-now">
        <h2 id="demo-now">Now</h2>
        <TaskRows items={[review, approval]} label="Current work" />
      </section>
      <section className="panel" aria-labelledby="demo-outcomes">
        <h2 id="demo-outcomes">Outcomes</h2>
        <ol className="event-list" role="list"><EventRow event={decisionEvent} showTask /></ol>
      </section>
    </section>
  )
}

export function GoalPage() {
  return (
    <section aria-labelledby="demo-page-title">
      <Heading actions={<a href="#reports">Back to Reports</a>}>Goal activity</Heading>
      <section className="panel" aria-labelledby="demo-goal">
        <h2 id="demo-goal">Keep the export easy to review in a spreadsheet</h2>
        <p><a href={knowledgeHref(knowledge.note.key)}>Read the decision record</a></p>
        <ol className="event-list" role="list">
          <EventRow event={decisionEvent} showTask />
        </ol>
      </section>
    </section>
  )
}
