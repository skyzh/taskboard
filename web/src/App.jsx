import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ArrowUpRight, ChevronDown, X } from 'lucide-react'
import { queryPath, useResource, watchBoardRevision, watchFrontendBuild } from './data.js'
import About from './About.jsx'
import BoardTour from './BoardTour.jsx'
import { useDemoTour, withoutTour } from './DemoTour.jsx'
import './demo-tour.css'
import BoardViews from './BoardViews.jsx'
import AssigneePage from './AssigneePage.jsx'
import GoalActivity, { goalActivityFragment } from './GoalActivity.jsx'
import Integrations from './Integrations.jsx'
import Knowledge, { knowledgeListHref } from './Knowledge.jsx'
import Notifications from './Notifications.jsx'
import MyActivity from './MyActivity.jsx'
import { ReportTime } from './ReportTime.jsx'
import { Outcomes, Overview } from './Overview.jsx'
import PersonalCodex from './PersonalCodex.jsx'
import Profile from './Profile.jsx'
import RegistrationDirectory from './RegistrationDirectory.jsx'
import Reports, { ReportsHeading } from './Reports.jsx'
import Schedules from './Schedules.jsx'
import Settings from './Settings.jsx'
import Stats from './Stats.jsx'
import { watchBoardSections } from './sectionIndex.js'
import { BoardBrand, notificationLogFilters, schedulesHref, signInHref } from './shared.jsx'
import { SearchResults, TaskSearch } from './TaskSearch.jsx'
import { KnowledgeRecordTools, TaskDetail, WorkLog, WorkQueue } from './Work.jsx'
import { useTheme } from './theme.js'
import {
  browserTimeZone, createTimeZoneFormatters, readTimeZonePreference, saveTimeZonePreference, TimeZoneContext,
} from './timeZone.js'

const welcomeBannerKey = 'tasks-board.reports-welcome-hidden'
const demoUrl = 'https://skyzh.github.io/taskboard/'

function readWelcomeBannerPreference() {
  try {
    return window.localStorage.getItem(welcomeBannerKey) !== 'true'
  } catch {
    return true
  }
}

function reportFragment(project, assigneeView, defaultProject = 'T') {
  const route = project === 'all' ? 'reports-all' : 'reports'
  const params = new URLSearchParams()
  if (project !== defaultProject && project !== 'all') params.set('project', project)
  if (assigneeView) params.set('view', assigneeView)
  return params.size ? `${route}?${params}` : route
}

function fragmentId(fragment, pattern) {
  const match = fragment.match(pattern)
  const id = match ? Number(match[1]) : null
  return Number.isSafeInteger(id) ? id : null
}

const scheduleAnchor = /^schedule-(R-[1-9]\d{0,18})$/

function useFragment(preview) {
  const [{ fragment: rawFragment, reveal }, setLocation] = useState(() => ({ fragment: window.location.hash.slice(1), reveal: true }))
  const fragment = preview ? withoutTour(rawFragment) : rawFragment
  useEffect(() => {
    function changed() {
      const fragment = window.location.hash.slice(1)
      setLocation(previous => ({
        fragment,
        reveal: !preview || withoutTour(previous.fragment) !== withoutTour(fragment),
      }))
    }
    window.addEventListener('hashchange', changed)
    return () => window.removeEventListener('hashchange', changed)
  }, [preview])

  useEffect(() => {
    if (!fragment || !reveal || rawFragment !== fragment) return
    const params = new URLSearchParams(fragment)
    const section = params.get('section')
    const base = fragment.split('?', 1)[0]
    let anchor = /^(?:task-|event-|activity$)/.test(base) || scheduleAnchor.test(base) ? base : fragment
    if (fragment.startsWith('notifications-')) anchor = 'notifications'
    if (base === 'reports-all' || base === 'reports') anchor = 'reports'
    if (fragment.startsWith('stats?')) anchor = 'stats'
    if (base === 'knowledge' || base === 'integrations') anchor = base
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
  }, [fragment, reveal, rawFragment])
  const writeFilterFragment = useCallback((next, replace) => {
    const hash = `#${next}`
    if (window.location.hash === hash) return
    if (replace) window.history.replaceState(window.history.state, '', hash)
    else window.history.pushState(window.history.state, '', hash)
    setLocation({ fragment: next, reveal: false })
  }, [])
  return [fragment, writeFilterFragment, rawFragment]
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

export default function App({ previewNotice, tourSteps } = {}) {
  const preview = Boolean(previewNotice)
  const [fragment, writeFilterFragment, rawFragment] = useFragment(preview)
  const demoTour = useDemoTour(rawFragment, tourSteps)
  const theme = useTheme()
  const main = useRef(null)
  const tourTrigger = useRef(null)
  const tourOrigin = useRef('')
  const restoreTourFocus = useRef(false)
  const [tour, setTour] = useState(null)
  const tourIndex = tour?.findIndex(step => step.route === fragment) ?? -1
  const tourStep = tour?.[tourIndex]
  useEffect(() => {
    if (tour && !tourStep) setTour(null)
  }, [tour, tourStep])
  useLayoutEffect(() => {
    if (restoreTourFocus.current && !tour) {
      restoreTourFocus.current = false
      const trigger = tourTrigger.current || main.current
      trigger?.focus({ preventScroll: true })
    }
  }, [tour])
  const [showWelcomeBanner, setShowWelcomeBanner] = useState(readWelcomeBannerPreference)
  const [welcomeExpanded, setWelcomeExpanded] = useState(false)
  function chooseWelcomeBanner(show) {
    setShowWelcomeBanner(show)
    try {
      if (show) window.localStorage.removeItem(welcomeBannerKey)
      else window.localStorage.setItem(welcomeBannerKey, 'true')
    } catch {
      // The preference still applies to the current page.
    }
  }
  const [timeZonePreference, setTimeZonePreference] = useState(readTimeZonePreference)
  const timeZone = timeZonePreference || browserTimeZone
  const timeZoneFormatters = useMemo(() => createTimeZoneFormatters(timeZone), [timeZone])
  const [taskAnchor, taskContext] = fragment.split('?', 2)
  const scheduleId = taskAnchor.match(scheduleAnchor)?.[1]
  const scheduleProject = scheduleId ? new URLSearchParams(taskContext).get('project') : null
  const recordHistory = new URLSearchParams(taskContext).get('history')
  const namedTask = taskAnchor.match(/^task-([A-Z][A-Z0-9]{0,7}-[1-9]\d{0,18})$/)?.[1]
  const selected = namedTask || fragmentId(taskAnchor, /^task-([1-9]\d*)$/)
  const fromSearch = selected ? new URLSearchParams(taskContext).get('from-search')?.trim() : ''
  const [ticketMove, setTicketMove] = useState(null)
  useEffect(() => {
    setTicketMove(previous => previous?.href === `#${taskAnchor}` ? previous : null)
  }, [taskAnchor])
  const [projectChoice, chooseProject] = useState(null)
  const reportsParams = ['reports', 'reports-all'].includes(taskAnchor) ? new URLSearchParams(taskContext) : null
  const requestedReportsProject = reportsParams?.get('project')
  const requestedAssigneeView = reportsParams?.get('view')
  const assigneeView = requestedAssigneeView === 'idle' ? 'all'
    : ['all', 'recent'].includes(requestedAssigneeView) ? requestedAssigneeView : 'active'
  const showGoalActivity = requestedAssigneeView === 'goal-activity'
  const goalAgent = reportsParams?.get('agent')
  const goalKey = reportsParams?.get('goal')
  const allReports = !fragment || taskAnchor === 'reports-all'
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
  const session = savedActor ? { ...sessionResource, data: { ...sessionResource.data, actor: savedActor } } : sessionResource
  const canManageRegistrations = !preview && !session.error && session.data?.actor?.kind === 'human' &&
    session.data?.can_manage_registrations === true
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
  const defaultProject = projectList.data?.default_project
    || visibleProjects?.find(entry => entry.prefix === 'T')?.prefix || visibleProjects?.[0]?.prefix || 'T'
  const preferredProject = projectChoice || defaultProject
  const projectsPending = !visibleProjects && !projectList.error && (sessionResource.pending || hasSession)
  const hasProjectChoices = visibleProjects?.length > 1
  const knowledgeFilters = taskAnchor === 'knowledge' ? new URLSearchParams(taskContext) : null
  const knowledgeScope = !preview && knowledgeFilters?.get('scope') === 'assigned' ? 'assigned' : 'board'
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
  else if (fragment === 'manage') view = 'manage'
  else if (taskAnchor === 'integrations') view = 'integrations'
  else if (fragment === 'personal-codex') view = 'personal-codex'
  else if (fragment === 'about') view = 'about'
  else if (taskAnchor === 'knowledge') view = 'knowledge'
  else if (scheduleId || params.has('schedules')) view = 'schedules'
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
    || defaultProject
  const supportsAllProjects = ['board', 'team-overview'].includes(view)
  const allProjectRead = supportsAllProjects && params.get('scope') === 'all'
  const project = view === 'reports' ? reportsProject : selected ? namedTask?.split('-')[0] || 'T'
    : logFilters?.project || scheduleProject || params.get('project') || preferredProject
  useEffect(() => {
    setQueueFilters(previous => previous.tag && previous.tag.project !== project
      ? { ...previous, tag: undefined }
      : previous)
  }, [project])
  const linkedOwner = view === 'board' && params.get('section') === 'queue-heading' && params.get('owner')
  const linkedQueue = linkedOwner ? fragment : null
  const fromList = view === 'task' && listReturn.current?.ticket === `#${taskAnchor}`
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
    !needsOidcSignIn && (!projectsPending || view === 'activity') && ['board', 'team-overview', 'activity'].includes(view)
      ? queryPath('/overview', allProjectRead ? { scope: 'all' } : { project: view === 'activity' ? undefined : project })
      : null,
    revision,
  )
  const integrationsParams = new URLSearchParams(view === 'integrations' ? taskContext : undefined)
  const integrationsProject = visibleProjects?.find(item => item.prefix === integrationsParams.get('project'))?.prefix
  const integrationsHref = allReports ? '#integrations' : `#integrations?project=${project}`
  const reportsScopeReady = allReports || !requestedReportsProject || Boolean(visibleProjects)
  const personalAgentsLink = reportsScopeReady && session.data?.actor?.kind === 'human'
    ? <a href={integrationsHref}>Personal Agents</a> : null
  const showAllReports = view === 'reports' && hasProjectChoices && allReports
  const reportsPending = Boolean((allReports || requestedReportsProject) && projectsPending)
  const reportsMode = reportsPending ? (fragment || 'reports-all')
    : reportFragment(allReports ? 'all' : reportsProject,
      ['active', 'all', 'idle', 'recent'].includes(requestedAssigneeView) ? assigneeView : undefined, defaultProject)
  const reportsHref = `#${view === 'reports' ? reportsMode : preferredReportsMode}`
  function startTour() {
    const taskRoute = new URLSearchParams({ project: reportsProject, section: 'queue-heading' }).toString()
    tourOrigin.current = fragment
    setTour([
      { route: fragment, target: 'reports', title: 'Reports', text: 'See who owns the current work, what needs a decision, and what was completed.' },
      { route: taskRoute, target: 'board', title: 'Tasks', text: 'Open a ticket to see its owner, current state, and next step.' },
      { route: 'activity', target: 'activity', title: 'Activity', text: 'See who reported progress, asked for help, or recorded an outcome, and when.' },
      { route: allReports ? 'stats' : `stats?project=${reportsProject}`, target: 'stats', title: 'Stats',
        text: 'Change the time period to see ticket activity over an hour, day, or week.' },
      ...(verified ? [
        { route: 'knowledge', target: 'knowledge', title: 'Knowledge',
          text: 'Find shared decisions and notes, with links back to the work they came from.' },
        { route: 'notifications-needs-me', target: 'notifications', title: 'Notifications',
          text: 'Open Needs me to find decisions and reviews waiting for you.' },
      ] : []),
      { route: integrationsHref.slice(1), target: 'integrations', title: 'Integrations',
        text: 'Choose how you want to work with an agent and copy the matching setup prompt.' },
    ])
  }
  function closeTour() {
    restoreTourFocus.current = true
    writeFilterFragment(tourOrigin.current, true)
    setTour(null)
  }
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
    if (hasSession && !preview) return watchBoardRevision(refresh, updateConnection, { websocket })
  }, [hasSession, preview, refresh, updateConnection, websocket])
  function chooseTimeZone(preference) {
    setTimeZonePreference(preference)
    saveTimeZonePreference(preference)
  }
  const projectPicker = hasProjectChoices && (
    <label className="project-switcher embedded-picker">
      <select aria-label="Project" value={showAllReports || allProjectRead ? 'all' : project} onChange={event => {
        const value = event.target.value
        if (supportsAllProjects) {
          if (value !== 'all') chooseProject(value)
          setQueueFilters(previous => ({ ...previous, tag: undefined }))
          const next = new URLSearchParams(value === 'all' ? { scope: 'all' } : { project: value })
          if (view === 'team-overview') next.set('section', 'team-overview')
          window.location.hash = next.toString()
          return
        }
        if (value === 'all') {
          window.location.hash = reportFragment('all', assigneeView)
          return
        }
        chooseProject(value)
        setQueueFilters({})
        setWaitingLabel('')
        window.location.hash = view === 'reports' ? showGoalActivity ? goalActivityFragment(value) : reportFragment(value, assigneeView, defaultProject)
          : view === 'team-overview' ? new URLSearchParams({ project: value, section: 'team-overview' }).toString()
          : view === 'schedules' ? schedulesHref(value)
          : view
      }}>
        {(view === 'reports' || supportsAllProjects) && <option value="all">All projects</option>}
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
            showNotifications={verified || session.data?.sign_in_available} showKnowledge={verified} />
          <TaskSearch query={search} fragment={fragment} />
          <div className={preview ? 'account account-with-tour' : 'account'}>
            {preview && <>
              {demoTour.toggle}
              <a className="board-repository" href="https://github.com/skyzh/taskboard" aria-label="Taskboard on GitHub"><span aria-hidden="true" /></a>
            </>}
            <Profile session={session} fragment={`#${fragment}`} theme={theme} canManageRegistrations={canManageRegistrations}
              freshness={hasSession && !preview && <BoardFreshness checkedAt={connection.checkedAt}
                source={connection.source} className="profile-freshness" />} preview={preview} />
          </div>
        </div>
        {hasSession && !preview && <BoardConnection {...connection} />}
        {demoTour.panel}
        {view === 'team-overview' && <SectionIndex key={project} overview={overview.data} />}
        {tourStep && <BoardTour step={tourStep} index={tourIndex} total={tour.length}
          onMove={index => writeFilterFragment(tour[index].route, false)} onClose={closeTour} />}
      </header>
      <main ref={main} tabIndex={0} aria-label="Taskboard content">
        <div className="main-content">
          {previewNotice && <p className="section-message">{previewNotice}</p>}
          {updateDeferred && <p className="board-connection" role="status">Taskboard will refresh when you finish your changes.</p>}
          {view === 'reports' && !showGoalActivity && showWelcomeBanner && (
            <aside className="reports-welcome" aria-labelledby="reports-welcome-title" data-expanded={welcomeExpanded || undefined}>
              <div>
                <h2 id="reports-welcome-title">
                  <span>Get started with Taskboard</span>
                  <button type="button" className="reports-welcome-toggle" aria-expanded={welcomeExpanded}
                    aria-controls="reports-welcome-body" onClick={() => setWelcomeExpanded(open => !open)}>
                    Get started with Taskboard <ChevronDown size={16} aria-hidden="true" />
                  </button>
                </h2>
                <div id="reports-welcome-body" className="reports-welcome-body">
                  <p>Taskboard is a shared workspace for people and agents. Track work in Tasks, see who’s doing what in
                    Reports, and find shared decisions in Knowledge.</p>
                  <div className="reports-welcome-actions">
                    <button type="button" ref={tourTrigger} className="reports-tour-trigger" disabled={projectsPending || Boolean(tourStep)}
                      onClick={preview ? demoTour.start : startTour}>Take the tour</button>
                    {!preview && <a href={demoUrl} target="_blank" rel="noreferrer"
                      aria-label="See the demo (opens the public example in a new tab)">
                      See the demo <ArrowUpRight size={16} aria-hidden="true" />
                    </a>}
                    {reportsScopeReady && <a href={integrationsHref}>Set up an agent</a>}
                  </div>
                  {reportsScopeReady && <p className="report-note">Raft agents register as Team DRIs when they first connect.
                    Use agent setup for personal agents or external AI factories.</p>}
                </div>
              </div>
              <button type="button" className="reports-welcome-dismiss" aria-label="Dismiss welcome banner" onClick={() => {
                main.current?.focus({ preventScroll: true })
                setWelcomeExpanded(false)
                chooseWelcomeBanner(false)
              }}><X size={18} aria-hidden="true" /></button>
            </aside>
          )}
          {(view === 'team-overview' || (view === 'reports' && showGoalActivity)) && projectPicker}
          {view === 'reports' && (reportsPending
            ? <section className="reports" aria-labelledby="reports">
              <ReportsHeading headingId="reports" projectPicker={projectPicker} />
              <p className="section-message" role="status">Loading Reports…</p>
            </section>
            : showGoalActivity
              ? <GoalActivity key={project} project={project} revision={revision} agent={goalAgent} goal={goalKey}
                onFilter={(agent, goal) => writeFilterFragment(goalActivityFragment(project, agent, goal))} />
              : <Reports revision={revision} project={projectsPending ? undefined : project}
                defaultProject={defaultProject} assigneeView={assigneeView}
                onAssigneeViewChange={value => writeFilterFragment(reportFragment(allReports ? 'all' : project, value, defaultProject), false)}
                projects={showAllReports ? visibleProjects : undefined} viewer={verified ? session.data.actor : null}
                headerAction={personalAgentsLink} projectPicker={projectPicker} />)}
          {view === 'stats' && <Stats revision={revision} period={statsPeriod} project={statsProject}
            projects={visibleProjects} />}
          {view === 'settings' && <Settings session={session} theme={theme} timeZonePreference={timeZonePreference}
            chooseTimeZone={chooseTimeZone} showWelcomeBanner={showWelcomeBanner} chooseWelcomeBanner={chooseWelcomeBanner}
            onProfileSaved={actor => { setSavedActor(actor); refresh() }}
            project={visibleProjects?.some(item => item.prefix === project) ? project : visibleProjects?.[0]?.prefix || 'T'}
            projects={visibleProjects} revision={revision} onProjectCreated={refresh} />}
          {view === 'manage' && <RegistrationDirectory available={canManageRegistrations} session={session} revision={revision} />}
          {view === 'about' && <About revision={revision} />}
          {view === 'integrations' && <Integrations session={session} project={integrationsProject}
            projects={visibleProjects} revision={revision}
            onProjectChange={value => writeFilterFragment(value ? `integrations?project=${value}` : 'integrations', true)}
            challenge={integrationsParams.get('cli')} preview={preview} />}
          {view === 'knowledge' && <Knowledge key={knowledgeFilters.get('entry') || 'index'}
            session={session} verified={verified} revision={revision} projects={visibleProjects} preview={preview}
            onChanged={refresh} RecordTools={KnowledgeRecordTools}
            scope={knowledgeScope} search={knowledgeFilters.get('q') || ''}
            entry={knowledgeFilters.get('entry')} history={knowledgeFilters.get('history')}
            onSearch={value => writeFilterFragment(knowledgeListHref(knowledgeScope, value).slice(1), false)}
            onScope={value => writeFilterFragment(knowledgeListHref(value, knowledgeFilters.get('q')).slice(1), false)} />}
          {view === 'personal-codex' && <PersonalCodex key={session.data?.actor?.id} session={session}
            reportsHref={reportsHref} />}
          {view === 'search' && <SearchResults query={search} revision={revision} />}
          {view === 'assignee' && <AssigneePage key={`${project}:${owner}`} owner={owner} section={params.get('section')} revision={revision} project={project}
            reportsHref={reportsHref} canEditGoal={verified && session.data.actor.id === owner}
            viewer={verified ? session.data.actor : null} />}
          {view === 'notifications' && <Notifications session={session} verified={verified} revision={revision}
            onChanged={refresh} fragment={fragment} project={project} projects={visibleProjects} />}
          {view === 'activity' && <WorkLog requested={linkedEvent} revision={revision} ownerProfiles={overview.data?.owner_profiles}
            fragment={fragment} onFiltersChange={writeFilterFragment} />}
          {view === 'my-activity' && <MyActivity revision={revision} />}
          {view === 'schedules' && <Schedules key={JSON.stringify([session.data?.actor?.id, scheduleId])}
            id={scheduleId} project={project} requestedProject={scheduleProject} projectPicker={projectPicker}
            revision={revision} session={session} verified={verified} onChanged={refresh} />}
          {view === 'board' && (
            <>
              {projectPicker && <div className="tasks-desktop-project">{projectPicker}</div>}
              <div className="page-heading tasks-heading"><h1 id="board">Tasks</h1>
                {verified && <a href={schedulesHref(project)}>Schedules</a>}
              </div>
              <WorkQueue revision={revision} overview={overview.data} filters={queueFilters} setFilters={setQueueFilters}
                waitingLabel={waitingLabel} setWaitingLabel={setWaitingLabel}
                allProjects={allProjectRead}
                project={projectsPending && !params.get('project') ? undefined : project} projectPicker={projectPicker}
                layout={queueLayout} setLayout={setQueueLayout} phoneColumn={phoneColumn} setPhoneColumn={setPhoneColumn}
                kanbanNavigation={kanbanNavigation} listNavigation={listNavigation} listReturn={listReturn} />
            </>
          )}
          {view === 'task' && <TaskDetail key={selected} id={selected} revision={revision} session={session}
            preferredProject={preferredProject} returnHref={fromList ? listReturn.current.board : undefined}
            fromSearch={fromSearch} history={recordHistory} onChanged={refresh}
            moveReceipt={ticketMove?.href === `#${taskAnchor}` ? ticketMove.receipt : undefined}
            onMoved={(href, receipt) => {
              setTicketMove({ href, receipt })
              window.location.hash = fromSearch ? `${href}?${new URLSearchParams({ 'from-search': fromSearch })}` : href
              refresh()
            }} />}
          {view === 'team-overview' && (
            <>
              <div className="page-heading"><h1 id="team-overview">Team overview</h1></div>
              <Overview key={allProjectRead ? 'all' : project} resource={overview} revision={revision} project={project}
                allProjects={allProjectRead} />
              <Outcomes key={allProjectRead ? 'all' : project} revision={revision} project={project} allProjects={allProjectRead} />
            </>
          )}
        </div>
      </main>
    </TimeZoneContext.Provider>
  )
}
