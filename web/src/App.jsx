import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { queryPath, useResource, watchBoardRevision, watchFrontendBuild } from './data.js'
import About from './About.jsx'
import BoardViews from './BoardViews.jsx'
import AssigneePage from './AssigneePage.jsx'
import GoalActivity, { goalActivityFragment } from './GoalActivity.jsx'
import Notifications from './Notifications.jsx'
import MyActivity from './MyActivity.jsx'
import { ReportTime } from './ReportTime.jsx'
import { Outcomes, Overview } from './Overview.jsx'
import Profile from './Profile.jsx'
import Reports, { ReportsHeading } from './Reports.jsx'
import Schedules from './Schedules.jsx'
import Settings from './Settings.jsx'
import Stats from './Stats.jsx'
import { watchBoardSections } from './sectionIndex.js'
import { BoardBrand, notificationLogFilters, signInHref } from './shared.jsx'
import { SearchResults, TaskSearch } from './TaskSearch.jsx'
import { TaskDetail, WorkLog, WorkQueue } from './Work.jsx'
import { useTheme } from './theme.js'
import {
  browserTimeZone, createTimeZoneFormatters, readTimeZonePreference, saveTimeZonePreference, TimeZoneContext,
} from './timeZone.js'

function reportFragment(project, assigneeView) {
  const params = new URLSearchParams()
  if (project !== 'T') params.set('project', project)
  if (assigneeView === 'all') params.set('view', 'all')
  return params.size ? `reports?${params}` : 'reports'
}

function fragmentId(fragment, pattern) {
  const match = fragment.match(pattern)
  const id = match ? Number(match[1]) : null
  return Number.isSafeInteger(id) ? id : null
}

function useFragment() {
  const [{ fragment, reveal }, setLocation] = useState(() => ({ fragment: window.location.hash.slice(1), reveal: true }))
  useEffect(() => {
    function changed() { setLocation({ fragment: window.location.hash.slice(1), reveal: true }) }
    window.addEventListener('hashchange', changed)
    return () => window.removeEventListener('hashchange', changed)
  }, [])

  useEffect(() => {
    if (!fragment || !reveal) return
    const params = new URLSearchParams(fragment)
    const section = params.get('section')
    const base = fragment.split('?', 1)[0]
    let anchor = /^(?:task-|event-|activity$)/.test(base) ? base : fragment
    if (fragment.startsWith('notifications-')) anchor = 'notifications'
    if (fragment === 'reports-all' || fragment.startsWith('reports?')) anchor = 'reports'
    if (fragment.startsWith('stats?')) anchor = 'stats'
    if (fragment === 'map' || fragment === 'workstreams-title') anchor = 'team-overview'
    const id = params.get('dri') ? section || 'assignee-title'
      : params.get('search')?.trim() ? 'search-results' : params.has('schedules') ? 'schedules' : section || anchor
    function revealAnchor() {
      const target = document.getElementById(id)
      if (!target) return false
      if (target.tabIndex >= 0 || target.hasAttribute('tabindex')) {
        target.focus({ preventScroll: true })
      }
      target.scrollIntoView({ block: 'start' })
      return true
    }
    if (revealAnchor()) return
    const observer = new MutationObserver(() => {
      if (revealAnchor()) observer.disconnect()
    })
    observer.observe(document.getElementById('root'), { childList: true, subtree: true })
    return () => observer.disconnect()
  }, [fragment, reveal])
  const writeFilterFragment = useCallback((next, replace) => {
    const hash = `#${next}`
    if (window.location.hash === hash) return
    if (replace) window.history.replaceState(window.history.state, '', hash)
    else window.history.pushState(window.history.state, '', hash)
    setLocation({ fragment: next, reveal: false })
  }, [])
  return [fragment, writeFilterFragment]
}

function SectionIndex({ overview }) {
  const nav = useRef(null)
  const [current, setCurrent] = useState(null)
  useEffect(() => watchBoardSections(nav.current, setCurrent), [])

  const now = overview?.now
  const decisions = overview?.decisions
  const links = [
    ['now-title', 'Now', now?.next_cursor == null ? now?.items.length : undefined],
    ['decisions-title', 'Needs a decision', decisions?.next_cursor == null ? decisions?.items.length : undefined],
    ['outcomes-title', 'Outcomes'],
  ]
  return (
    <nav ref={nav} className="section-index" aria-label="Board sections">
      {links.map(([id, label, count]) => (
        <a key={id} href={`#${id}`} aria-current={current === id ? 'location' : undefined}>
          {label}{count != null && <span className="index-count">{count}</span>}
        </a>
      ))}
    </nav>
  )
}

function BoardFreshness({ checkedAt, source, className }) {
  if (!checkedAt) return null
  const socket = source === 'socket'
  return (
    <span className={className}>
      {socket ? 'Updated at ' : 'Checked at '}
      <ReportTime at={checkedAt} />
    </span>
  )
}

function BoardConnection({ checkedAt, checking, stale, source }) {
  const status = stale ? 'Updates interrupted' : !checkedAt && checking ? 'Checking for updates…' : ''
  if (!checkedAt && !status) return null

  return (
    <div className="appbar-connection">
      <span role="status">{status}</span>
      <BoardFreshness checkedAt={checkedAt} source={source} className="appbar-freshness" />
    </div>
  )
}

export default function App() {
  const [fragment, writeFilterFragment] = useFragment()
  const theme = useTheme()
  const [timeZonePreference, setTimeZonePreference] = useState(readTimeZonePreference)
  const timeZone = timeZonePreference || browserTimeZone
  const timeZoneFormatters = useMemo(() => createTimeZoneFormatters(timeZone), [timeZone])
  const [taskAnchor, taskContext] = fragment.split('?', 2)
  const namedTask = taskAnchor.match(/^task-([A-Z][A-Z0-9]{0,7}-[1-9]\d{0,18})$/)?.[1]
  const selected = namedTask || fragmentId(taskAnchor, /^task-([1-9]\d*)$/)
  const fromSearch = selected ? new URLSearchParams(taskContext).get('from-search')?.trim() : ''
  const [preferredProject, chooseProject] = useState('T')
  const reportsParams = taskAnchor === 'reports' ? new URLSearchParams(taskContext) : null
  const requestedReportsProject = reportsParams?.get('project')
  const requestedAssigneeView = reportsParams?.get('view')
  const initialAssigneeView = ['all', 'idle'].includes(requestedAssigneeView) ? 'all' : 'active'
  const showGoalActivity = requestedAssigneeView === 'goal-activity'
  const goalAgent = reportsParams?.get('agent')
  const goalKey = reportsParams?.get('goal')
  const allReports = fragment === 'reports-all'
  const [preferredReportsMode, rememberReportsMode] = useState(allReports ? 'reports-all' : 'reports')
  const linkedEvent = fragmentId(taskAnchor, /^event-([1-9]\d*)-log$/)
  const [queueFilters, setQueueFilters] = useState({})
  const [queueLayout, setQueueLayout] = useState('list')
  const [phoneColumn, setPhoneColumn] = useState('ready')
  const kanbanNavigation = useRef({})
  const listNavigation = useRef({})
  const listReturn = useRef(null)
  const [waitingLabel, setWaitingLabel] = useState('')
  const [appliedQueueLink, setAppliedQueueLink] = useState(null)
  const [revision, setRevision] = useState(0)
  const [connection, setConnection] = useState({ stale: false })
  const [updateDeferred, setUpdateDeferred] = useState(false)
  useEffect(() => watchFrontendBuild(setUpdateDeferred), [])
  const sessionResource = useResource('/session')
  const websocket = sessionResource.data?.websocket_revisions === true
  const [savedActor, setSavedActor] = useState(null)
  const session = savedActor ? { ...sessionResource, data: { actor: savedActor } } : sessionResource
  const hasSession = Boolean(session.data?.actor)
  const needsOidcSignIn = !hasSession && session.data?.sign_in_provider === 'oidc'
  const appHeader = useRef(null)
  useLayoutEffect(() => {
    const header = appHeader.current
    if (!header) return
    const style = document.documentElement.style
    function reserveHeaderSpace() {
      style.setProperty('--app-header-height', `${header.getBoundingClientRect().height}px`)
    }
    reserveHeaderSpace()
    const resize = new ResizeObserver(reserveHeaderSpace)
    resize.observe(header, { box: 'border-box' })
    return () => {
      resize.disconnect()
      style.removeProperty('--app-header-height')
    }
  }, [needsOidcSignIn])
  const isAgent = session.data?.actor?.kind === 'agent'
  const verified = ['agent', 'human'].includes(session.data?.actor?.kind)
  const params = new URLSearchParams(fragment)
  const logFilters = notificationLogFilters(fragment)
  const projectList = useResource(hasSession ? '/projects' : null, revision)
  const visibleProjects = projectList.data?.items
  const hasProjectChoices = visibleProjects?.length > 1
  const statsFilters = taskAnchor === 'stats' ? new URLSearchParams(taskContext) : null
  const requestedStatsPeriod = statsFilters?.get('period')
  const statsPeriod = ['hour', 'week'].includes(requestedStatsPeriod) ? requestedStatsPeriod : 'day'
  const requestedStatsProject = statsFilters?.get('project')
  const statsProject = /^[A-Z][A-Z0-9]{0,7}$/.test(requestedStatsProject || '') ? requestedStatsProject : undefined
  const owner = params.get('dri')
  const search = selected ? '' : params.get('search')?.trim() || ''
  let view = 'board'
  if (owner) view = 'assignee'
  else if (search) view = 'search'
  else if (!fragment || taskAnchor === 'reports' || allReports) view = 'reports'
  else if (fragment === 'settings') view = 'settings'
  else if (fragment === 'about') view = 'about'
  else if (params.has('schedules')) view = 'schedules'
  else if (taskAnchor === 'stats') view = 'stats'
  else if (logFilters || ['notifications', 'notifications-needs-me'].includes(fragment)) view = 'notifications'
  else if (fragment === 'my-activity' && isAgent) view = 'my-activity'
  else if (linkedEvent || taskAnchor === 'activity' || fragment === 'work-log-title') view = 'activity'
  else if (selected) view = 'task'
  else if (params.get('section') === 'team-overview' ||
    ['team-overview', 'now-title', 'decisions-title', 'outcomes-title', 'workstreams-title', 'map'].includes(fragment)) {
    view = 'team-overview'
  }
  const reportsProject = visibleProjects?.find(entry => entry.prefix === requestedReportsProject)?.prefix
    || visibleProjects?.find(entry => entry.prefix === 'T')?.prefix || visibleProjects?.[0]?.prefix || 'T'
  const project = view === 'reports' ? reportsProject : selected ? namedTask?.split('-')[0] || 'T'
    : logFilters?.project || params.get('project') || preferredProject
  useEffect(() => {
    setQueueFilters(previous => previous.tag && previous.tag.project !== project
      ? { ...previous, tag: undefined }
      : previous)
  }, [project])
  const linkedOwner = view === 'board' && params.get('section') === 'queue-heading' && params.get('owner')
  const linkedQueue = linkedOwner ? fragment : null
  const fromList = view === 'task' && listReturn.current?.ticket === `#${fragment}`
  // Apply a new queue link before the work list loads with the previous filters.
  if (appliedQueueLink !== linkedQueue) {
    setAppliedQueueLink(linkedQueue)
    if (linkedOwner && listReturn.current?.board !== `#${fragment}`) {
      setQueueFilters({ dri: linkedOwner })
      setWaitingLabel('')
    }
  }
  useEffect(() => {
    if (view !== 'board' && !fromList) listReturn.current = null
  }, [view, fromList])
  const overview = useResource(
    !needsOidcSignIn && ['board', 'team-overview', 'activity'].includes(view)
      ? queryPath('/overview', { project: view === 'activity' ? undefined : project })
      : null,
    revision,
  )
  const showAllReports = view === 'reports' && hasProjectChoices && allReports
  const reportsPending = Boolean((allReports || requestedReportsProject) && !visibleProjects &&
    (sessionResource.pending || (hasSession && !projectList.error)))
  const reportsMode = allReports ? 'reports-all'
    : reportsPending ? fragment : reportFragment(reportsProject, initialAssigneeView)
  const reportsHref = `#${view === 'reports' ? reportsMode : preferredReportsMode}`
  useEffect(() => {
    if (view === 'reports' && !reportsPending) {
      rememberReportsMode(reportsMode)
      if (requestedReportsProject && visibleProjects && requestedReportsProject !== reportsProject) {
        window.location.replace(`#${showGoalActivity ? goalActivityFragment(reportsProject) : reportsMode}`)
      }
    }
  }, [view, reportsPending, reportsMode, requestedReportsProject, visibleProjects, reportsProject, showGoalActivity])
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
    <label className="project-switcher embedded-picker">
      <select aria-label="Project" value={showAllReports ? 'all' : project} onChange={event => {
        const value = event.target.value
        if (value === 'all') {
          window.location.hash = 'reports-all'
          return
        }
        chooseProject(value)
        setQueueFilters({})
        setWaitingLabel('')
        window.location.hash = view === 'reports' ? showGoalActivity ? goalActivityFragment(value) : reportFragment(value)
          : view === 'team-overview' ? new URLSearchParams({ project: value, section: 'team-overview' }).toString()
          : view
      }}>
        {view === 'reports' && <option value="all">All projects</option>}
        {visibleProjects.map(entry => <option key={entry.prefix} value={entry.prefix}>{entry.title}</option>)}
      </select>
      <ChevronDown size={14} aria-hidden="true" focusable="false" />
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
      <header ref={appHeader} className={view === 'board' ? 'app-header tasks-header' : 'app-header'}>
        <div className="masthead">
          <BoardBrand href={reportsHref} />
          <BoardViews current={activeTab} reportsHref={reportsHref} isAgent={isAgent}
            showNotifications={verified || session.data?.sign_in_available} />
          <TaskSearch query={search} fragment={fragment} />
          <div className="account">
            <Profile session={session} fragment={`#${fragment}`} theme={theme}
              freshness={hasSession && <BoardFreshness checkedAt={connection.checkedAt}
                source={connection.source} className="profile-freshness" />} />
          </div>
        </div>
        {hasSession && <BoardConnection {...connection} />}
        {view === 'team-overview' && <SectionIndex key={project} overview={overview.data} />}
      </header>
      <main tabIndex={0} aria-label="Taskboard content">
        <div className="main-content">
          {updateDeferred && <p className="board-connection" role="status">Taskboard will refresh when you finish your changes.</p>}
          {['reports', 'team-overview'].includes(view) && projectPicker}
          {view === 'reports' && (reportsPending
            ? <section className="reports" aria-labelledby="reports">
              <ReportsHeading headingId="reports" />
              <p className="section-message" role="status">Loading Reports…</p>
            </section>
            : showGoalActivity
              ? <GoalActivity key={project} project={project} revision={revision} agent={goalAgent} goal={goalKey}
                onFilter={(agent, goal) => writeFilterFragment(goalActivityFragment(project, agent, goal))} />
              : <Reports revision={revision} project={project} initialAssigneeView={initialAssigneeView}
                projects={showAllReports ? visibleProjects : undefined} viewer={verified ? session.data.actor : null} />)}
          {view === 'stats' && <Stats revision={revision} period={statsPeriod} project={statsProject}
            projects={visibleProjects} />}
          {view === 'settings' && <Settings session={session} theme={theme} timeZonePreference={timeZonePreference}
            chooseTimeZone={chooseTimeZone} onProfileSaved={actor => { setSavedActor(actor); refresh() }}
            project={visibleProjects?.some(item => item.prefix === project) ? project : visibleProjects?.[0]?.prefix || 'T'}
            projects={visibleProjects} revision={revision} />}
          {view === 'about' && <About revision={revision} />}
          {view === 'search' && <SearchResults query={search} revision={revision} />}
          {view === 'assignee' && <AssigneePage key={`${project}:${owner}`} owner={owner} section={params.get('section')} revision={revision} project={project}
            reportsHref={reportsHref} canEditGoal={verified && session.data.actor.id === owner}
            viewer={verified ? session.data.actor : null} />}
          {view === 'notifications' && <Notifications session={session} verified={verified} revision={revision}
            onChanged={refresh} fragment={fragment} project={project} projects={visibleProjects} />}
          {view === 'activity' && <WorkLog requested={linkedEvent} revision={revision} ownerProfiles={overview.data?.owner_profiles}
            fragment={fragment} onFiltersChange={writeFilterFragment} />}
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
                kanbanNavigation={kanbanNavigation} listNavigation={listNavigation} listReturn={listReturn} />
            </>
          )}
          {view === 'task' && <TaskDetail key={selected} id={selected} revision={revision} session={session}
            preferredProject={preferredProject} returnHref={fromList ? listReturn.current.board : undefined}
            fromSearch={fromSearch} onChanged={refresh} />}
          {view === 'team-overview' && (
            <>
              <div className="page-heading"><h1 id="team-overview">Team overview</h1></div>
              <Overview key={project} resource={overview} revision={revision} project={project} />
              <Outcomes key={project} revision={revision} project={project} />
            </>
          )}
        </div>
      </main>
    </TimeZoneContext.Provider>
  )
}
