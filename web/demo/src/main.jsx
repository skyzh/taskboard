import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import BoardViews from '../../src/BoardViews.jsx'
import { DemoUnavailable } from '../../src/DemoControl.jsx'
import { ReportTimeProvider } from '../../src/ReportTime.jsx'
import { AssigneeReportList, ReportsHeading, ReportsLayout } from '../../src/Reports.jsx'
import { QueuedAsksContent } from '../../src/QueuedAsks.jsx'
import { AssigneeAvatar, BoardBrand, eventHref, ownerHref } from '../../src/shared.jsx'
import { useTheme } from '../../src/theme.js'
import {
  browserTimeZone, createTimeZoneFormatters, readTimeZonePreference, saveTimeZonePreference, TimeZoneContext,
} from '../../src/timeZone.js'
import DemoNotice from './DemoNotice.jsx'
import { ActivityPage, TaskPage, TasksPage } from './DemoWork.jsx'
import {
  AboutPage, GoalPage, InboxPage, IntegrationsPage, KnowledgePage, OverviewPage, SchedulesPage, SettingsPage, StatsPage,
} from './DemoPages.jsx'
import {
  capturedAt, decision, decisionEvent, events, knowledge, knowledgeHref, profiles, project, reports, repository,
  requestLog, setupGuide, tasks,
} from './fixture.js'
import '../../src/styles.css'
import '../../src/night.css'
import '../../src/kanban.css'
import './snapshot.css'

const ownerRoute = ownerHref('avery', undefined, project.key)
const goalRoute = '#reports?view=goal-activity&project=T&agent=avery'
const scheduleRoute = '#schedules=&project=T'

const tour = [
  { href: '#reports', title: 'Reports', text: 'Find current work and decisions by owner.' },
  { href: ownerRoute, title: 'Avery’s report', text: 'See the export choices and Avery’s next decision.' },
  { href: '#board', title: 'Tasks', text: 'Switch between List and Kanban to see the same set of tickets.' },
  { href: decision.public_url, title: 'A recorded decision', text: 'Read the outcome, the checklist, and the follow-up request.' },
  { href: eventHref(decisionEvent.id), title: 'Linked Activity', text: 'Open the dated record for the decision and see who made it.' },
  { href: '#stats', title: 'Stats', text: 'Compare activity across time periods.' },
  { href: '#notifications', title: 'Notifications', text: 'Find decisions, requests, and their receipts.' },
  { href: knowledgeHref(knowledge.note.key), title: 'Knowledge', text: 'Read the decision alongside its source and staged follow-up.' },
  { href: '#my-activity', title: 'Juniper’s agent view', text: 'See how a coding agent finds assigned work and related activity.' },
  { href: scheduleRoute, title: 'Schedules', text: 'Follow a recurring check to its latest ticket.' },
  { href: '#settings', title: 'Settings', text: 'Try display preferences and see the sample profile.' },
  { href: '#about', title: 'About', text: 'Find the project source and learn what this demo shows.' },
  { href: '#integrations', title: 'Personal Agents', text: 'Open the setup guide to connect agents to your own board.', action: setupGuide },
].map((step, index) => ({
  ...step,
  location: step.href + (step.href.includes('?') ? '&' : '?') + 'tour=' + (index + 1),
}))

function withoutTour(hash) {
  return tour.find(step => step.location === hash)?.href || hash
}

function readRoute(hash) {
  const [path, query] = hash.slice(1).split('?', 2)
  const options = new URLSearchParams(query)
  const parameters = new URLSearchParams(path)
  const taskKey = /^task-(?:T-)?(\d+)$/.exec(path)?.[1]
  const eventId = /^event-(\d+)-log$/.exec(path)?.[1]
  const task = taskKey && tasks.find(task => task.id === Number(taskKey))
  const event = eventId && events.find(event => event.id === Number(eventId))
  if (task) return { view: 'task', tab: 'board', task }
  if (event) return { view: 'activity', tab: 'activity', event }
  if (parameters.has('dri') && profiles.has(parameters.get('dri'))) {
    return { view: 'assignee', tab: 'reports', owner: parameters.get('dri') }
  }
  if (parameters.has('search')) return { view: 'search', tab: 'board', query: parameters.get('search').trim() }
  if (parameters.has('schedules')) return { view: 'schedules', tab: 'board' }
  if (path === 'board' || parameters.get('project') === project.key) {
    return { view: 'board', tab: 'board', owner: parameters.get('owner') }
  }
  if (['settings', 'integrations', 'about', 'activity', 'my-activity', 'team-overview'].includes(path)) {
    return { view: path, tab: path === 'team-overview' ? 'reports' : path }
  }
  if (path === 'knowledge') return { view: path, tab: path, entry: options.get('entry'), query: options.get('q') || '' }
  if (path === 'stats') return { view: path, tab: path, period: ['hour', 'week'].includes(options.get('period')) ? options.get('period') : 'day' }
  if (['notifications', 'notifications-needs-me', 'notifications-log'].includes(path)) {
    return { view: 'notifications', tab: 'notifications', inbox: path }
  }
  if (options.get('view') === 'goal-activity') return { view: 'goal-activity', tab: 'reports' }
  if (!path || path === 'reports' || path === 'reports-all') return { view: 'reports', tab: 'reports', all: path === 'reports-all' }
  return { view: 'missing', tab: '' }
}

function ReportsView({ owner, all, startTour, trigger, tourOpen }) {
  const person = profiles.get(owner)
  const entries = owner ? reports.filter(entry => entry.owner === owner) : reports
  return (
    <section className="reports" aria-labelledby="reports">
      <ReportsHeading headingId="reports" focusable>
        {owner ? <a href="#reports">Back to Reports</a> : <button type="button" className="snapshot-tour-trigger" ref={trigger}
          onClick={startTour} hidden={tourOpen}>Start tour</button>}
      </ReportsHeading>
      {person ? <div className="demo-links">
        <h2>{person.name}’s report</h2>
        <a href={`#project=${project.key}&section=queue-heading&owner=${owner}`}>All {person.name}’s tasks</a>
        <a href={owner === 'avery' ? goalRoute : '#my-activity'}>{owner === 'avery' ? 'Goal activity' : 'My activity example'}</a>
      </div> : <nav className="demo-links" aria-label="Reports views">
        <a href="#reports" aria-current={!all ? 'page' : undefined}>Project</a>
        <a href="#reports-all" aria-current={all ? 'page' : undefined}>All projects</a>
        <a href="#team-overview">Team overview</a>
        <a href={goalRoute}>Goal activity</a>
      </nav>}
      <ReportsLayout sidebar={!owner && <QueuedAsksContent page={{ data: { items: requestLog } }} />}>
        <AssigneeReportList entries={entries} label={person ? `${person.name}’s work` : 'Inventory Export reports'} publicPreview />
      </ReportsLayout>
    </section>
  )
}

function DemoAccount({ hash, agent }) {
  const details = useRef(null)
  const accountButton = useRef(null)
  const profile = profiles.get(agent ? 'juniper' : 'avery')
  useEffect(() => {
    details.current.open = false
  }, [hash])
  return (
    <details className="demo-account account" ref={details} onKeyDown={event => {
      if (event.key === 'Escape' && details.current.open) {
        event.stopPropagation()
        details.current.open = false
        accountButton.current.focus()
      }
    }}>
      <summary ref={accountButton} aria-label={`${profile.name}’s account menu`}>
        <AssigneeAvatar name={profile.name} /> <span>{profile.name}</span>
      </summary>
      <div className="demo-account-menu">
        <a href="#settings">Settings</a>
        <a href="#integrations">Integrations</a>
        <a href="#about">About</a>
        <a href="#my-activity">Juniper’s agent view</a>
        <DemoUnavailable>Sign in</DemoUnavailable>
      </div>
    </details>
  )
}

function Demo() {
  const [hash, setHash] = useState(() => window.location.hash)
  const [layout, setLayout] = useState('list')
  const [column, setColumn] = useState('ready')
  const theme = useTheme()
  const [timeZonePreference, setTimeZonePreference] = useState(readTimeZonePreference)
  const timeZone = timeZonePreference || browserTimeZone
  const timeZoneFormatters = useMemo(() => createTimeZoneFormatters(timeZone), [timeZone])
  const header = useRef(null)
  const content = useRef(null)
  const trigger = useRef(null)
  const tourPanel = useRef(null)
  const restoreFocus = useRef(false)
  const backLinks = useRef(new Map())
  const step = tour.findIndex(item => item.location === hash)
  const active = tour[step]
  const route = readRoute(active?.href || hash)

  useEffect(() => {
    function changed(event) {
      const from = withoutTour(new URL(event.oldURL).hash) || '#reports'
      const to = new URL(event.newURL).hash
      const origin = readRoute(from)
      const target = readRoute(withoutTour(to))
      if (target.task && ['reports', 'assignee', 'board', 'search', 'notifications', 'schedules', 'knowledge'].includes(origin.view)) {
        backLinks.current.set(target.task.id, { href: from, label: {
          reports: 'Back to Reports', assignee: 'Back to report', board: 'Back to Tasks', search: 'Back to Search results',
          notifications: 'Back to Notifications', schedules: 'Back to Schedules', knowledge: 'Back to Knowledge',
        }[origin.view] })
      }
      setHash(to)
    }
    window.addEventListener('hashchange', changed)
    return () => window.removeEventListener('hashchange', changed)
  }, [])

  useLayoutEffect(() => {
    const element = header.current
    const style = document.documentElement.style
    const reserveSpace = () => style.setProperty('--app-header-height', `${element.getBoundingClientRect().height}px`)
    reserveSpace()
    const resize = new ResizeObserver(reserveSpace)
    resize.observe(element)
    return () => {
      resize.disconnect()
      style.removeProperty('--app-header-height')
    }
  }, [])

  useLayoutEffect(() => {
    if (active) {
      tourPanel.current?.focus({ preventScroll: true })
    } else if (restoreFocus.current && trigger.current) {
      restoreFocus.current = false
      trigger.current.focus({ preventScroll: true })
    } else if (hash) {
      const target = content.current.querySelector('[aria-current="location"]')
        || content.current.querySelector('#demo-knowledge-entry')
        || content.current.querySelector('h1')
      target?.focus({ preventScroll: true })
    }
    window.scrollTo(0, 0)
  }, [hash, active])

  function moveTour(next) {
    window.location.hash = tour[next].location
  }

  function finishTour() {
    restoreFocus.current = true
    window.location.hash = '#reports'
  }

  function chooseTimeZone(value) {
    setTimeZonePreference(value)
    saveTimeZonePreference(value)
  }

  const source = route.task && backLinks.current.get(route.task.id)
  const backHref = source?.href || '#board'
  const backLabel = source?.label || 'Back to Tasks'
  return (
    <TimeZoneContext.Provider value={timeZoneFormatters}>
      <ReportTimeProvider capturedAt={capturedAt}>
        <header className="app-header" ref={header}>
          <div className="masthead demo-masthead">
            <BoardBrand href="#reports" />
            <BoardViews current={route.tab} reportsHref="#reports" showNotifications showKnowledge
              isAgent={route.view === 'my-activity'} />
            <div className="demo-header-actions">
              <DemoAccount hash={hash} agent={route.view === 'my-activity'} />
              <a className="demo-repository" href={repository} aria-label="Taskboard on GitHub">
                <span aria-hidden="true" />
              </a>
            </div>
          </div>
          {active && (
            <aside className="snapshot-tour" aria-labelledby="tour-title" tabIndex={-1} ref={tourPanel} onKeyDown={event => {
              if (event.key === 'Escape') finishTour()
            }}>
              <h2 id="tour-title">{active.title} <span>{step + 1} of {tour.length}</span></h2>
              <p>{active.text}</p>
              {active.action && <p><a href={active.action}>Open the agent setup guide</a></p>}
              <div className="snapshot-tour-actions">
                <button type="button" disabled={step === 0} onClick={() => moveTour(step - 1)}>Back</button>
                <button type="button" onClick={() => step + 1 === tour.length ? finishTour() : moveTour(step + 1)}>
                  {step + 1 === tour.length ? 'Finish' : 'Next'}
                </button>
                {step + 1 < tour.length && <button type="button" onClick={finishTour}>Skip</button>}
              </div>
            </aside>
          )}
        </header>
        <main ref={content} aria-label="Taskboard content">
          <div className="main-content snapshot">
            <div className="demo-project-heading">
              <p className="snapshot-project">{project.title} <span>Demo project</span></p>
              <form className="demo-search" role="search" aria-label="Search tasks" key={route.query || ''} onSubmit={event => {
                event.preventDefault()
                const query = new FormData(event.currentTarget).get('search').trim()
                if (query) window.location.hash = new URLSearchParams({ search: query }).toString()
              }}>
                <label>Search tasks <input type="search" name="search" defaultValue={route.view === 'search' ? route.query : ''} /></label>
                <button type="submit">Search</button>
              </form>
            </div>
            {['reports', 'assignee'].includes(route.view) && <ReportsView owner={route.owner} all={route.all}
              startTour={() => moveTour(0)} trigger={trigger} tourOpen={Boolean(active)} />}
            {['board', 'search'].includes(route.view) && <TasksPage layout={layout} chooseLayout={setLayout}
              column={column} chooseColumn={setColumn} owner={route.owner} query={route.query} />}
            {route.view === 'task' && <TaskPage key={route.task.id} task={route.task} backHref={backHref} backLabel={backLabel} />}
            {route.view === 'activity' && <ActivityPage key={route.event?.id || 'all'} selected={route.event?.id} />}
            {route.view === 'my-activity' && <ActivityPage agent />}
            {route.view === 'team-overview' && <OverviewPage />}
            {route.view === 'goal-activity' && <GoalPage />}
            {route.view === 'stats' && <StatsPage period={route.period} />}
            {route.view === 'notifications' && <InboxPage tab={route.inbox} />}
            {route.view === 'knowledge' && <KnowledgePage entry={route.entry} query={route.query} />}
            {route.view === 'schedules' && <SchedulesPage />}
            {route.view === 'settings' && <SettingsPage theme={theme} timeZonePreference={timeZonePreference} chooseTimeZone={chooseTimeZone} />}
            {route.view === 'integrations' && <IntegrationsPage />}
            {route.view === 'about' && <AboutPage />}
            {route.view === 'missing' && <section className="panel">
              <h1 tabIndex={-1}>This page isn’t in the demo</h1>
              <p><a href="#reports">Back to Reports</a> · <a href={repository}>Taskboard on GitHub</a></p>
            </section>}
          </div>
        </main>
      </ReportTimeProvider>
    </TimeZoneContext.Provider>
  )
}

createRoot(document.getElementById('root')).render(<DemoNotice><Demo /></DemoNotice>)
