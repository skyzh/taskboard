import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { BoardLink, useBoardLocation, useContentTitle } from './BoardNavigation.jsx'
import { legacyLocation, routeAddress } from './boardRoutes.js'
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
import Reports, { ReportsHeading } from './Reports.jsx'
import Schedules from './Schedules.jsx'
import Settings from './Settings.jsx'
import Stats from './Stats.jsx'
import { watchBoardSections } from './sectionIndex.js'
import { BoardBrand, notificationLogFilters, ResourceStatus, schedulesHref, useSignInHref } from './shared.jsx'
import { SearchResults, TaskSearch } from './TaskSearch.jsx'
import { KnowledgeRecordTools, TaskDetail, WorkLog, WorkQueue } from './Work.jsx'
import { useTheme } from './theme.js'
import { projectFilters, projectHref, validProject } from './projectScope.js'
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

function settingsFragment(project, section) {
  const query = new URLSearchParams()
  if (section) query.set('section', section)
  if (project) query.set('project', project)
  return query.size ? `#settings?${query}` : '#settings'
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
  const board = useBoardLocation()
  const rawFragment = board?.rawFragment ?? window.location.hash.slice(1)
  const fragment = preview ? withoutTour(rawFragment) : rawFragment
  const reveal = board?.reveal ?? true
  const explicitAnchor = board?.location.hash.slice(1)
  useEffect(() => {
    if ((!fragment && !explicitAnchor) || !reveal || rawFragment !== fragment) return
    const [base, query] = fragment.split('?', 2)
    const params = new URLSearchParams(query ?? fragment)
    const section = base === 'settings' ? new URLSearchParams(query).get('section') : params.get('section')
    let anchor = /^(?:task-|event-|activity$)/.test(base) || scheduleAnchor.test(base) ? base : fragment
    if (fragment.startsWith('notifications-')) anchor = 'notifications'
    if (base === 'reports-all' || base === 'reports') anchor = 'reports'
    if (['board', 'stats', 'knowledge', 'integrations', 'settings', 'notifications', 'my-activity', 'team-overview'].includes(base)) anchor = base
    if (['map', 'workstreams-title', 'now-title', 'decisions-title', 'outcomes-title', 'work-log-title', 'queue-heading'].includes(base)) anchor = base === 'map' || base === 'workstreams-title' ? 'team-overview' : base
    const id = explicitAnchor || (params.get('dri') ? section || 'assignee-title'
      : params.get('search')?.trim() ? 'search-results' : params.has('schedules') ? 'schedules' : section || anchor)
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
  }, [fragment, reveal, rawFragment, explicitAnchor])
  return [fragment, rawFragment]
}

function SectionIndex({ overview, scope }) {
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
        <BoardLink key={id} href={projectHref(`#${id}`, scope)} aria-current={current === id ? 'location' : undefined}>
          {label}{count != null && <span className="index-count">{count}</span>}
        </BoardLink>
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
  const board = useBoardLocation()
  const [fragment, rawFragment] = useFragment(preview)
  const navigate = board?.navigate
  const login = useSignInHref(`#${fragment}`)
  const writeFilterFragment = (next, replace) => navigate?.(next, { replace, reveal: false })
  const demoTour = useDemoTour(rawFragment, tourSteps, navigate)
  const theme = useTheme()
  const main = useRef(null)
  const tourTrigger = useRef(null)
  const tourOrigin = useRef('')
  const restoreTourFocus = useRef(false)
  const [tour, setTour] = useState(null)
  const tourLocation = tour && legacyLocation(fragment)
  const tourIndex = tourLocation ? tour.findIndex(step =>
    routeAddress(legacyLocation(step.route)) === routeAddress(tourLocation)) : -1
  const tourStep = tour?.[tourIndex]
  useEffect(() => {
    if (tour && !tourStep) setTour(null)
  }, [tour, tourStep])
  useLayoutEffect(() => {
    if (!restoreTourFocus.current || tour) return
    const current = legacyLocation(fragment)
    if (!current || routeAddress(current) !== routeAddress(legacyLocation(tourOrigin.current))) return
    restoreTourFocus.current = false
    const trigger = tourTrigger.current || main.current
    trigger?.focus({ preventScroll: true })
  }, [fragment, tour])
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
  const [lastScope, rememberScope] = useState(null)
  const reportsParams = ['reports', 'reports-all'].includes(taskAnchor) ? new URLSearchParams(taskContext) : null
  const requestedReportsProject = reportsParams?.get('project')
  const requestedAssigneeView = reportsParams?.get('view')
  const assigneeView = requestedAssigneeView === 'idle' ? 'all'
    : ['all', 'recent', 'watching'].includes(requestedAssigneeView) ? requestedAssigneeView : 'active'
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
  const params = new URLSearchParams(taskContext ?? fragment)
  const logFilters = notificationLogFilters(fragment)
  const projectList = useResource(hasSession ? '/projects' : null, revision)
  const visibleProjects = projectList.data?.items
  const defaultProject = projectList.data?.default_project
    || visibleProjects?.find(entry => entry.prefix === 'T')?.prefix || visibleProjects?.[0]?.prefix || 'T'
  const projectsPending = !visibleProjects && !projectList.error && (sessionResource.pending || hasSession)
  const hasProjectChoices = visibleProjects?.length > 1
  const knowledgeFilters = taskAnchor === 'knowledge' ? new URLSearchParams(taskContext) : null
  const knowledgeScope = !preview && knowledgeFilters?.get('scope') === 'assigned' ? 'assigned' : 'board'
  const knowledgeOrder = !preview && knowledgeFilters?.get('order') === 'updated' ? 'updated' : ''
  const statsFilters = taskAnchor === 'stats' ? new URLSearchParams(taskContext) : null
  const requestedStatsPeriod = statsFilters?.get('period')
  const statsPeriod = ['hour', 'week'].includes(requestedStatsPeriod) ? requestedStatsPeriod : 'day'
  const owner = params.get('dri')
  const search = selected ? '' : params.get('search')?.trim() || ''
  let view = 'board'
  if (board) {
    const recordViews = { home: 'reports', event: 'activity', entry: 'knowledge', schedule: 'schedules' }
    view = recordViews[board.route] || board.route
  } else {
    if (owner) view = 'assignee'
    else if (search) view = 'search'
    else if (!fragment || taskAnchor === 'reports' || allReports) view = 'reports'
    else if (taskAnchor === 'settings' || taskAnchor === 'manage') view = 'settings'
    else if (taskAnchor === 'integrations') view = 'integrations'
    else if (fragment === 'personal-codex') view = 'personal-codex'
    else if (fragment === 'about') view = 'about'
    else if (taskAnchor === 'knowledge') view = 'knowledge'
    else if (scheduleId || params.has('schedules')) view = 'schedules'
    else if (taskAnchor === 'stats') view = 'stats'
    else if (logFilters || ['notifications', 'notifications-needs-me'].includes(taskAnchor)) view = 'notifications'
    else if (taskAnchor === 'my-activity' && isAgent) view = 'my-activity'
    else if (linkedEvent || taskAnchor === 'activity' || taskAnchor === 'work-log-title') view = 'activity'
    else if (selected) view = 'task'
    else if (params.get('section') === 'team-overview' ||
      ['team-overview', 'now-title', 'decisions-title', 'outcomes-title', 'workstreams-title', 'map'].includes(taskAnchor)) {
      view = 'team-overview'
    }
  }
  useContentTitle(view === 'reports' && (showGoalActivity ? 'Goal activity' : assigneeView === 'watching' ? 'Watching' : null))
  const supportsAllProjects = ['reports', 'board', 'activity', 'team-overview', 'knowledge', 'stats', 'notifications', 'my-activity'].includes(view) ||
    (view === 'schedules' && !scheduleId)
  const scopedView = supportsAllProjects || ['assignee', 'task', 'schedules'].includes(view)
  const canSelectProject = scopedView || ['settings', 'integrations'].includes(view)
  const knowledgeEntry = knowledgeFilters?.get('entry')
  const recordProject = namedTask?.split('-')[0] || knowledgeEntry?.match(/^([A-Z][A-Z0-9]{0,7})-\d+$/)?.[1]
  const requestedProject = recordProject || (canSelectProject ? params.get('project') : null)
  const defaultsToAll = ['activity', 'knowledge', 'stats', 'notifications', 'my-activity'].includes(view) && !logFilters
  const allProjectRead = !requestedProject && supportsAllProjects &&
    (allReports || params.get('scope') === 'all' || defaultsToAll)
  const allGoalActivity = showGoalActivity && allProjectRead
  // Legacy list links without a scope keep their board default across history navigation.
  const project = requestedProject || defaultProject
  const projectUnavailable = !recordProject && requestedProject && (!validProject(requestedProject)
    || (visibleProjects && !visibleProjects.some(entry => entry.prefix === requestedProject)))
  const scopePending = Boolean(requestedProject && !recordProject && canSelectProject && !preview &&
    (projectsPending || (hasSession && projectList.error && !visibleProjects)))
  const scope = scopedView && !projectUnavailable
    ? allProjectRead ? 'all' : (requestedProject || (visibleProjects && project)) : undefined
  const navigationScope = scopedView ? scope : requestedProject && !projectUnavailable ? requestedProject : lastScope
  const projectRead = allProjectRead ? undefined : project
  const reportsProject = project
  const settingsProject = visibleProjects?.find(item => item.prefix === project)?.prefix || defaultProject
  const settingsHref = settingsFragment(navigationScope && navigationScope !== 'all' ? navigationScope : null)
  useEffect(() => {
    if (scope && !scopePending && (visibleProjects || scope === 'all')) rememberScope(scope)
    else if (!scopedView && requestedProject && !projectUnavailable && visibleProjects) rememberScope(requestedProject)
  }, [scope, scopePending, scopedView, requestedProject, projectUnavailable, visibleProjects])
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
    !needsOidcSignIn && !projectUnavailable && !scopePending && (!projectsPending || view === 'activity') && ['board', 'team-overview', 'activity'].includes(view)
      ? queryPath('/overview', projectFilters(projectRead))
      : null,
    revision,
  )
  const integrationsParams = new URLSearchParams(view === 'integrations' ? taskContext : undefined)
  const integrationsProject = visibleProjects?.find(item => item.prefix === integrationsParams.get('project'))?.prefix
  const integrationsHref = allProjectRead ? '#integrations' : projectHref('#integrations', project)
  const reportsScopeReady = allReports || !requestedReportsProject || Boolean(visibleProjects)
  const personalAgentsLink = reportsScopeReady && session.data?.actor?.kind === 'human'
    ? <BoardLink href={integrationsHref}>Personal Agents</BoardLink> : null
  const showAllReports = view === 'reports' && hasProjectChoices && allProjectRead
  const reportsPending = Boolean((allReports || requestedReportsProject) && projectsPending)
  const reportsMode = reportsPending ? (fragment || 'reports-all')
    : reportFragment(allProjectRead ? 'all' : reportsProject,
      ['active', 'all', 'idle', 'recent', 'watching'].includes(requestedAssigneeView) ? assigneeView : undefined,
      requestedReportsProject ? null : defaultProject)
  const reportsHref = view === 'reports' ? `#${reportsMode}`
    : projectHref(`#${preferredReportsMode}`, navigationScope, { replace: true })
  const knowledgeProject = allProjectRead ? undefined : scope
  const scopedRoute = href => projectHref(href, navigationScope)
  const reportsReturn = new URLSearchParams(preferredReportsMode.split('?', 2)[1])
  const watchingReturn = reportsReturn.get('view') === 'watching' ? `#${preferredReportsMode}` : null
  const savedProfileReturn = window.history?.state?.watchingProfileReturn
  const savedWatchingReturn = savedProfileReturn?.fragment === fragment ? savedProfileReturn.href : null
  const profileReportsHref = savedWatchingReturn || watchingReturn || reportsHref
  useEffect(() => {
    if (view !== 'assignee' || !watchingReturn || savedWatchingReturn || preview) return
    // Preserve the return across reload on this profile's history entry.
    window.history.replaceState({
      ...window.history.state, watchingProfileReturn: { fragment, href: watchingReturn },
    }, '')
  }, [view, preview, fragment, watchingReturn, savedWatchingReturn])
  function startTour() {
    const taskRoute = scopedRoute('#section=queue-heading').slice(1)
    tourOrigin.current = fragment
    setTour([
      { route: fragment, target: 'reports', title: 'Reports', text: 'See who owns the current work, what needs a decision, and what was completed.' },
      { route: taskRoute, target: 'board', title: 'Tasks', text: 'Open a ticket to see its owner, current state, and next step.' },
      { route: scopedRoute('#activity').slice(1), target: 'activity', title: 'Activity', text: 'See who reported progress, asked for help, or recorded an outcome, and when.' },
      { route: scopedRoute('#stats').slice(1), target: 'stats', title: 'Stats',
        text: 'Change the time period to see ticket activity over an hour, day, or week.' },
      ...(verified ? [
        { route: scopedRoute('#knowledge').slice(1), target: 'knowledge', title: 'Knowledge',
          text: 'Find shared decisions and notes, with links back to the work they came from.' },
        { route: scopedRoute('#notifications-needs-me').slice(1), target: 'notifications', title: 'Notifications',
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
    if (view === 'reports' && !reportsPending && !projectUnavailable && !showGoalActivity) rememberReportsMode(reportsMode)
  }, [view, reportsPending, reportsMode, projectUnavailable, showGoalActivity])
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
      <select aria-label="Project" value={allProjectRead ? 'all' : project} onChange={event => {
        const value = event.target.value
        if (view === 'board') setQueueFilters(previous => ({ ...previous, tag: undefined }))
        const route = fragment ? `#${fragment}` : '#reports-all'
        navigate(view === 'schedules'
          ? schedulesHref(value === 'all' ? undefined : value, value === 'all')
          : projectHref(route, value, { replace: true }))
      }}>
        {supportsAllProjects && <option value="all">All projects</option>}
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
          <a href={login}>Sign in with OpenID Connect</a>
        </div>
      </main>
    )
  }
  return (
    <TimeZoneContext.Provider value={timeZoneFormatters}>
      <header ref={appHeader} className={view === 'board' ? 'app-header tasks-header' : 'app-header'}>
        <div className="masthead">
          <BoardBrand href={reportsHref} />
          <BoardViews current={activeTab} reportsHref={reportsHref} projectScope={navigationScope} isAgent={isAgent}
            showNotifications={verified || session.data?.sign_in_available} showKnowledge={verified} />
          <TaskSearch query={search} fragment={fragment} />
          <div className={preview ? 'account account-with-tour' : 'account'}>
            {preview && <>
              {demoTour.toggle}
              <a className="board-repository" href="https://github.com/skyzh/taskboard" aria-label="Taskboard on GitHub"><span aria-hidden="true" /></a>
            </>}
            <Profile session={session} fragment={`#${fragment}`} theme={theme} settingsHref={settingsHref}
              freshness={hasSession && !preview && <BoardFreshness checkedAt={connection.checkedAt}
                source={connection.source} className="profile-freshness" />} preview={preview} />
          </div>
        </div>
        {hasSession && !preview && <BoardConnection {...connection} />}
        {demoTour.panel}
        {view === 'team-overview' && !projectUnavailable && <SectionIndex key={project} overview={overview.data} scope={navigationScope} />}
        {tourStep && <BoardTour step={tourStep} index={tourIndex} total={tour.length}
          onMove={index => writeFilterFragment(tour[index].route, false)} onClose={closeTour} />}
      </header>
      <main ref={main} tabIndex={0} aria-label="Taskboard content">
        <div className="main-content">
          {previewNotice && <p className="section-message">{previewNotice}</p>}
          {updateDeferred && <p className="board-connection" role="status">Taskboard will refresh when you finish your changes.</p>}
          {projectUnavailable ? <section aria-label="Project">
            <p className="section-message" role="status">Project unavailable.</p>
            <BoardLink href={projectHref(`#${fragment}`, supportsAllProjects ? 'all' : defaultProject, { replace: true })}>
              {supportsAllProjects ? 'All projects' : 'Open the default project'}
            </BoardLink>
          </section> : scopePending ? <ResourceStatus resource={sessionResource.pending ? sessionResource : projectList} label="projects" /> : <>
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
                    {reportsScopeReady && <BoardLink href={integrationsHref}>Set up an agent</BoardLink>}
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
              ? <GoalActivity key={allGoalActivity ? 'all' : project} project={allGoalActivity ? null : project}
                revision={revision} agent={goalAgent} goal={goalKey}
                onFilter={(agent, goal) => writeFilterFragment(goalActivityFragment(allGoalActivity ? null : project, agent, goal))} />
              : <Reports revision={revision} project={projectsPending ? undefined : project}
                assigneeView={assigneeView}
                onAssigneeViewChange={value => writeFilterFragment(reportFragment(allProjectRead ? 'all' : project, value, requestedReportsProject ? null : defaultProject), false)}
                projects={showAllReports ? visibleProjects : undefined} viewer={verified ? session.data.actor : null}
                headerAction={personalAgentsLink} projectPicker={projectPicker} />)}
          {view === 'stats' && <Stats revision={revision} period={statsPeriod} project={scope === 'all' ? undefined : scope}
            projects={visibleProjects} />}
          {view === 'settings' && <Settings session={session} theme={theme} timeZonePreference={timeZonePreference}
            chooseTimeZone={chooseTimeZone} showWelcomeBanner={showWelcomeBanner} chooseWelcomeBanner={chooseWelcomeBanner}
            onProfileSaved={actor => { setSavedActor(actor); refresh() }}
            project={visibleProjects ? settingsProject : undefined} fragment={`#${fragment}`}
            projects={visibleProjects} revision={revision} onProjectCreated={refresh}
            onPermissionProjectChange={value => {
              writeFilterFragment(settingsFragment(value, 'permissions').slice(1), true)
            }} />}
          {view === 'about' && <About revision={revision} />}
          {view === 'integrations' && <Integrations session={session} project={integrationsProject}
            projects={visibleProjects} revision={revision}
            onProjectChange={value => writeFilterFragment(value ? `integrations?project=${value}` : 'integrations', true)}
            challenge={integrationsParams.get('cli')} preview={preview} />}
          {view === 'knowledge' && <Knowledge key={knowledgeFilters.get('entry') || 'index'}
            session={session} verified={verified} revision={revision} projects={visibleProjects} preview={preview} projectPicker={projectPicker}
            onChanged={refresh} RecordTools={KnowledgeRecordTools}
            scope={knowledgeScope} search={knowledgeFilters.get('q') || ''} order={knowledgeOrder} project={knowledgeProject}
            entry={knowledgeFilters.get('entry')} history={knowledgeFilters.get('history')}
            onSearch={value => writeFilterFragment(knowledgeListHref(knowledgeScope, value,
              { order: knowledgeOrder, project: knowledgeProject }).slice(1), false)}
            onScope={value => writeFilterFragment(knowledgeListHref(value, knowledgeFilters.get('q'),
              { order: knowledgeOrder, project: knowledgeProject }).slice(1), false)}
            onOrder={value => writeFilterFragment(knowledgeListHref(knowledgeScope, knowledgeFilters.get('q'),
              { order: value, project: knowledgeProject }).slice(1), false)} />}
          {view === 'personal-codex' && <PersonalCodex key={session.data?.actor?.id} session={session}
            reportsHref={reportsHref} />}
          {view === 'search' && <SearchResults query={search} revision={revision} />}
          {view === 'assignee' && <AssigneePage key={`${project}:${owner}`} owner={owner} section={params.get('section')} revision={revision} project={project}
            reportsHref={profileReportsHref} canEditGoal={verified && session.data.actor.id === owner}
            viewer={verified ? session.data.actor : null} onWatchChanged={refresh} />}
          {view === 'notifications' && <Notifications session={session} verified={verified} revision={revision}
            onChanged={refresh} fragment={fragment} project={projectRead} projectPicker={projectPicker} projects={visibleProjects} />}
          {view === 'activity' && <WorkLog requested={linkedEvent} revision={revision} ownerProfiles={overview.data?.owner_profiles}
            fragment={fragment} project={projectRead} projectPicker={projectPicker} onFiltersChange={writeFilterFragment} />}
          {view === 'my-activity' && (isAgent
            ? <MyActivity revision={revision} project={projectRead} projectPicker={projectPicker} />
            : <p className="section-message">My activity is available to signed-in agents.</p>)}
          {view === 'not-found' && <section className="page-heading">
            <h1>Page unavailable</h1><BoardLink href="#reports-all">Open Reports</BoardLink>
          </section>}
          {view === 'schedules' && <Schedules key={JSON.stringify([session.data?.actor?.id, scheduleId])}
            id={scheduleId} project={project} requestedProject={scheduleProject} projectPicker={projectPicker}
            allProjects={allProjectRead} revision={revision} session={session} verified={verified} onChanged={refresh} />}
          {view === 'board' && (
            <>
              {projectPicker && <div className="tasks-desktop-project">{projectPicker}</div>}
              <div className="page-heading tasks-heading"><h1 id="board">Tasks</h1>
                {verified && <BoardLink href={schedulesHref(allProjectRead ? undefined : project, allProjectRead)}>Schedules</BoardLink>}
              </div>
              <WorkQueue revision={revision} overview={overview.data} filters={queueFilters} setFilters={setQueueFilters}
                waitingLabel={waitingLabel} setWaitingLabel={setWaitingLabel}
                allProjects={allProjectRead}
                project={projectsPending && !requestedProject ? undefined : project} projectPicker={projectPicker}
                layout={queueLayout} setLayout={setQueueLayout} phoneColumn={phoneColumn} setPhoneColumn={setPhoneColumn}
                kanbanNavigation={kanbanNavigation} listNavigation={listNavigation} listReturn={listReturn} fragment={fragment} />
            </>
          )}
          {view === 'task' && <TaskDetail key={selected} id={selected} revision={revision} session={session}
            preferredProject={defaultProject} returnHref={fromList ? listReturn.current.board : undefined}
            fromSearch={fromSearch} history={recordHistory} onChanged={refresh}
            moveReceipt={ticketMove?.href === `#${taskAnchor}` ? ticketMove.receipt : undefined}
            onMoved={(href, receipt) => {
              setTicketMove({ href, receipt })
              navigate(fromSearch ? `${href}?${new URLSearchParams({ 'from-search': fromSearch })}` : href)
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
          </>}
        </div>
      </main>
    </TimeZoneContext.Provider>
  )
}
