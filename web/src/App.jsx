import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { queryPath, useResource, watchBoardRevision, watchFrontendBuild } from './data.js'
import About from './About.jsx'
import AssigneePage from './AssigneePage.jsx'
import Notifications from './Notifications.jsx'
import MyActivity from './MyActivity.jsx'
import { Outcomes, Overview } from './Overview.jsx'
import Profile from './Profile.jsx'
import Reports from './Reports.jsx'
import Schedules from './Schedules.jsx'
import Settings from './Settings.jsx'
import Stats from './Stats.jsx'
import { watchBoardSections } from './sectionIndex.js'
import { notificationLogFilters, signInHref, taskHref } from './shared.jsx'
import { SearchResults, TaskSearch } from './TaskSearch.jsx'
import { TaskDetail, WorkLog, WorkQueue } from './Work.jsx'
import { useTheme } from './theme.js'
import {
  browserTimeZone, createTimeZoneFormatters, readTimeZonePreference, saveTimeZonePreference, TimeZoneContext, useTimeZone,
} from './timeZone.js'

function fragmentId(fragment, pattern) {
  const match = fragment.match(pattern)
  const id = match ? Number(match[1]) : null
  return Number.isSafeInteger(id) ? id : null
}

function useFragment() {
  const [fragment, setFragment] = useState(() => window.location.hash.slice(1))
  useEffect(() => {
    function changed() { setFragment(window.location.hash.slice(1)) }
    window.addEventListener('hashchange', changed)
    return () => window.removeEventListener('hashchange', changed)
  }, [])

  useEffect(() => {
    if (!fragment) return
    const params = new URLSearchParams(fragment)
    const section = params.get('section')
    let anchor = fragment
    if (fragment.startsWith('notifications-')) anchor = 'notifications'
    if (fragment === 'reports-all') anchor = 'reports'
    const id = params.get('dri') ? section || 'assignee-title'
      : params.get('search')?.trim() ? 'search-results' : params.has('schedules') ? 'schedules' : section || anchor
    function reveal() {
      const target = document.getElementById(id)
      if (!target) return false
      if (target.tabIndex >= 0 || target.hasAttribute('tabindex')) {
        target.focus({ preventScroll: true })
      }
      target.scrollIntoView({ block: 'start' })
      return true
    }
    if (reveal()) return
    const observer = new MutationObserver(() => {
      if (reveal()) observer.disconnect()
    })
    observer.observe(document.getElementById('root'), { childList: true, subtree: true })
    return () => observer.disconnect()
  }, [fragment])
  return fragment
}

function BoardViews({ current, reportsHref, isAgent, showNotifications }) {
  const nav = useRef(null)
  useEffect(() => {
    const strip = nav.current
    function revealActive() {
      if (strip.scrollWidth <= strip.clientWidth) return
      const activeLink = strip.querySelector('[aria-current="page"]')
      if (!activeLink) return

      // Reveal a clipped active tab without scrolling the page away from the route's heading.
      const { left, right } = activeLink.getBoundingClientRect()
      const bounds = strip.getBoundingClientRect()
      if (left < bounds.left) strip.scrollLeft += left - bounds.left
      else if (right > bounds.right) strip.scrollLeft += right - bounds.right
    }

    revealActive()
    window.addEventListener('resize', revealActive)
    return () => window.removeEventListener('resize', revealActive)
  }, [current, isAgent, showNotifications])

  const links = [
    ['reports', 'Reports'],
    ['board', 'Tasks'],
    ['activity', 'Activity'],
    ['stats', 'Stats'],
    ...(showNotifications ? [['notifications', 'Notifications']] : []),
    ...(isAgent ? [['my-activity', 'My activity']] : []),
  ]
  return (
    <nav ref={nav} className="board-views" aria-label="Taskboard views">
      {links.map(([id, label]) => (
        <a key={id} href={id === 'reports' ? reportsHref : `#${id}`} aria-current={current === id ? 'page' : undefined}>{label}</a>
      ))}
    </nav>
  )
}

function SectionIndex({ overview, mapAvailable, openMap }) {
  const nav = useRef(null)
  const [current, setCurrent] = useState(null)
  useEffect(() => watchBoardSections(nav.current, setCurrent), [mapAvailable])

  const now = overview?.now
  const decisions = overview?.decisions
  const links = [
    ['now-title', 'Now', now?.next_cursor == null ? now?.items.length : undefined],
    ['decisions-title', 'Needs a decision', decisions?.next_cursor == null ? decisions?.items.length : undefined],
    ['outcomes-title', 'Outcomes'],
    ...(mapAvailable ? [['workstreams-title', 'Workstreams']] : []),
  ]
  return (
    <nav ref={nav} className="section-index" aria-label="Board sections">
      {links.map(([id, label, count]) => (
        <a key={id} href={`#${id}`} aria-current={current === id ? 'location' : undefined}
          onClick={id === 'workstreams-title' ? openMap : undefined}>
          {label}{count != null && <span className="index-count">{count}</span>}
        </a>
      ))}
    </nav>
  )
}

function BoardConnection({ checkedAt, checking, stale, source }) {
  const { formatClock, formatTimestamp } = useTimeZone()
  const socket = source === 'socket'
  const status = stale ? 'Updates interrupted' : !checkedAt && checking ? 'Checking for updates…' : ''
  if (!checkedAt && !status) return null

  return (
    <p className="appbar-connection">
      <span role="status">{status}</span>
      {checkedAt && (
        <span title={(socket ? 'Last update received ' : 'Last checked ') + formatTimestamp(checkedAt)}>
          {socket ? 'Updated ' : 'Checked '}
          <time dateTime={checkedAt}>{formatClock(checkedAt)}</time>
        </span>
      )}
    </p>
  )
}

export default function App({ MilestoneMap }) {
  const fragment = useFragment()
  const theme = useTheme()
  const [timeZonePreference, setTimeZonePreference] = useState(readTimeZonePreference)
  const timeZone = timeZonePreference || browserTimeZone
  const timeZoneFormatters = useMemo(() => createTimeZoneFormatters(timeZone), [timeZone])
  const namedTask = fragment.match(/^task-([A-Z][A-Z0-9]{0,7}-[1-9]\d{0,18})$/)?.[1]
  const selected = namedTask || fragmentId(fragment, /^task-([1-9]\d*)$/)
  const [preferredProject, chooseProject] = useState('T')
  const reportsMode = fragment === 'reports-all' ? 'reports-all' : 'reports'
  const [preferredReportsMode, rememberReportsMode] = useState(reportsMode)
  const linkedEvent = fragmentId(fragment, /^event-([1-9]\d*)-log$/)
  const [queueFilters, setQueueFilters] = useState({})
  const [queueLayout, setQueueLayout] = useState('list')
  const [phoneColumn, setPhoneColumn] = useState('ready')
  const kanbanNavigation = useRef({})
  const [waitingLabel, setWaitingLabel] = useState('')
  const [appliedQueueLink, setAppliedQueueLink] = useState(null)
  const [revision, setRevision] = useState(0)
  const [connection, setConnection] = useState({ stale: false })
  const [updateDeferred, setUpdateDeferred] = useState(false)
  useEffect(() => watchFrontendBuild(setUpdateDeferred), [])
  const [mapOpen, setMapOpen] = useState(fragment === 'map' || fragment === 'workstreams-title')
  useEffect(() => {
    if (fragment === 'map' || fragment === 'workstreams-title') setMapOpen(true)
  }, [fragment])
  const sessionResource = useResource('/session')
  const websocket = sessionResource.data?.websocket_revisions === true
  const [savedActor, setSavedActor] = useState(null)
  const session = savedActor ? { ...sessionResource, data: { actor: savedActor } } : sessionResource
  const hasSession = Boolean(session.data?.actor)
  const needsOidcSignIn = !hasSession && session.data?.sign_in_provider === 'oidc'
  const isAgent = session.data?.actor?.kind === 'agent'
  const verified = ['agent', 'human'].includes(session.data?.actor?.kind)
  const params = new URLSearchParams(fragment)
  const logFilters = notificationLogFilters(fragment)
  const project = selected ? namedTask?.split('-')[0] || 'T' : logFilters?.project || params.get('project') || preferredProject
  useEffect(() => {
    setQueueFilters(previous => previous.tag && previous.tag.project !== project
      ? { ...previous, tag: undefined }
      : previous)
  }, [project])
  const owner = params.get('dri')
  const search = params.get('search')?.trim() || ''
  let view = 'board'
  if (owner) view = 'assignee'
  else if (search) view = 'search'
  else if (!fragment || fragment === reportsMode) view = 'reports'
  else if (fragment === 'settings') view = 'settings'
  else if (fragment === 'about') view = 'about'
  else if (params.has('schedules')) view = 'schedules'
  else if (fragment === 'stats') view = 'stats'
  else if (logFilters || ['notifications', 'notifications-needs-me'].includes(fragment)) view = 'notifications'
  else if (fragment === 'my-activity' && isAgent) view = 'my-activity'
  else if (linkedEvent || ['activity', 'work-log-title'].includes(fragment)) view = 'activity'
  else if (selected) view = 'task'
  else if (['team-overview', 'now-title', 'decisions-title', 'outcomes-title', 'workstreams-title', 'map'].includes(fragment)) {
    view = 'team-overview'
  }
  const linkedOwner = view === 'board' && params.get('section') === 'queue-heading' && params.get('owner')
  const linkedQueue = linkedOwner ? fragment : null
  // Apply a new queue link before the work list loads with the previous filters.
  if (appliedQueueLink !== linkedQueue) {
    setAppliedQueueLink(linkedQueue)
    if (linkedOwner) {
      setQueueFilters({ dri: linkedOwner })
      setWaitingLabel('')
    }
  }
  const overview = useResource(
    !needsOidcSignIn && ['board', 'team-overview', 'activity'].includes(view)
      ? queryPath('/overview', { project: view === 'activity' ? undefined : project })
      : null,
    revision,
  )
  const projectList = useResource(hasSession ? '/projects' : null, revision)
  const visibleProjects = projectList.data?.items
  const hasProjectChoices = visibleProjects?.length > 1
  const showAllReports = view === 'reports' && hasProjectChoices && reportsMode === 'reports-all'
  const reportsHref = `#${view === 'reports' ? reportsMode : preferredReportsMode}`
  useEffect(() => {
    if (view === 'reports') rememberReportsMode(reportsMode)
  }, [view, reportsMode])
  const activeTab = ['assignee', 'team-overview'].includes(view) ? 'reports' : ['task', 'schedules'].includes(view) ? 'board' : view
  const refresh = useCallback(() => setRevision(value => value + 1), [])
  const updateConnection = useCallback(status => {
    setConnection(previous => ({ ...previous, ...status }))
  }, [])
  useEffect(() => {
    if (hasSession) return watchBoardRevision(refresh, updateConnection, { websocket })
  }, [hasSession, refresh, updateConnection, websocket])
  function chooseTimeZone(preference) {
    setTimeZonePreference(preference)
    saveTimeZonePreference(preference)
  }
  const projectPicker = hasProjectChoices && (
    <label>
      <span>Project</span>
      <select value={showAllReports ? 'all' : project} onChange={event => {
        const value = event.target.value
        if (value === 'all') {
          window.location.hash = 'reports-all'
          return
        }
        chooseProject(value)
        setQueueFilters({})
        setWaitingLabel('')
        window.location.hash = view
      }}>
        {view === 'reports' && <option value="all">All projects</option>}
        {visibleProjects.map(entry => <option key={entry.prefix} value={entry.prefix}>{entry.title}</option>)}
      </select>
    </label>
  )
  if (needsOidcSignIn) {
    return (
      <main aria-label="Taskboard content">
        <div className="main-content">
          <h1>Taskboard</h1>
          <a href={signInHref(`#${fragment}`)}>Sign in with OpenID Connect</a>
        </div>
      </main>
    )
  }
  return (
    <TimeZoneContext.Provider value={timeZoneFormatters}>
      <header className={view === 'board' ? 'app-header tasks-header' : 'app-header'}>
        <div className="masthead">
          <a className="brand" href={reportsHref}><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>Taskboard</a>
          <BoardViews current={activeTab} reportsHref={reportsHref} isAgent={isAgent}
            showNotifications={verified || session.data?.sign_in_available} />
          <TaskSearch query={search} fragment={fragment} />
          <div className="account">
            <Profile session={session} fragment={`#${fragment}`} theme={theme} />
          </div>
        </div>
        {hasSession && <BoardConnection {...connection} />}
        {view === 'team-overview' && <SectionIndex key={project} overview={overview.data} mapAvailable={Boolean(MilestoneMap)} openMap={() => setMapOpen(true)} />}
      </header>
      <main tabIndex={0} aria-label="Taskboard content">
        <div className="main-content">
          {updateDeferred && <p className="board-connection" role="status">Taskboard will refresh when you finish your changes.</p>}
          {['reports', 'team-overview'].includes(view) && projectPicker}
          {view === 'reports' && (
            <Reports revision={revision} project={project}
              projects={showAllReports ? visibleProjects : undefined} viewer={verified ? session.data.actor : null} />
          )}
          {view === 'stats' && <Stats revision={revision} />}
          {view === 'settings' && <Settings session={session} theme={theme} timeZonePreference={timeZonePreference}
            chooseTimeZone={chooseTimeZone} onProfileSaved={actor => { setSavedActor(actor); refresh() }} />}
          {view === 'about' && <About revision={revision} />}
          {view === 'search' && <SearchResults query={search} revision={revision} />}
          {view === 'assignee' && <AssigneePage key={`${project}:${owner}`} owner={owner} section={params.get('section')} revision={revision} project={project}
            reportsHref={reportsHref} canEditGoal={verified && session.data.actor.id === owner} />}
          {view === 'notifications' && <Notifications session={session} verified={verified} revision={revision}
            onChanged={refresh} fragment={fragment} project={project} projects={visibleProjects} />}
          {view === 'activity' && <WorkLog requested={linkedEvent} revision={revision} ownerProfiles={overview.data?.owner_profiles} />}
          {view === 'my-activity' && <MyActivity revision={revision} />}
          {view === 'schedules' && <Schedules key={project} project={project} revision={revision}
            session={session} verified={verified} onChanged={refresh} />}
          {view === 'board' && (
            <>
              {projectPicker && <div className="tasks-desktop-project">{projectPicker}</div>}
              <div className="page-heading tasks-heading"><h1 id="board">Tasks</h1>
                {verified && <a href={'#' + new URLSearchParams({ schedules: '', project })}>Schedules</a>}
              </div>
              <WorkQueue revision={revision} overview={overview.data} filters={queueFilters} setFilters={setQueueFilters}
                waitingLabel={waitingLabel} setWaitingLabel={setWaitingLabel} project={project} projectPicker={projectPicker}
                layout={queueLayout} setLayout={setQueueLayout} phoneColumn={phoneColumn} setPhoneColumn={setPhoneColumn}
                kanbanNavigation={kanbanNavigation} />
            </>
          )}
          {view === 'task' && <TaskDetail key={selected} id={selected} revision={revision} session={session}
            preferredProject={preferredProject} onChanged={refresh} />}
          {view === 'team-overview' && (
            <>
              <div className="page-heading"><h1 id="team-overview">Team overview</h1></div>
              <Overview key={project} resource={overview} revision={revision} project={project} />
              <Outcomes key={project} revision={revision} project={project} />
              {MilestoneMap && (
                <section id="map" className="panel workstreams" aria-labelledby="workstreams-title" tabIndex={-1}>
                  <h2 id="workstreams-title">Workstreams</h2>
                  <details className="map-disclosure" open={mapOpen} onToggle={event => setMapOpen(event.currentTarget.open)}>
                    <summary>Dependency map</summary>
                    {mapOpen && <MilestoneMap taskHref={taskHref} revision={revision} project={project} />}
                  </details>
                </section>
              )}
            </>
          )}
        </div>
      </main>
    </TimeZoneContext.Provider>
  )
}
