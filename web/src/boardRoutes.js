export const boardRoutes = [
  { id: 'home', path: '/', title: 'Reports' },
  { id: 'reports', path: '/reports', title: 'Reports' },
  { id: 'assignee', path: '/reports/agents/:owner', title: 'Agent report' },
  { id: 'board', path: '/tasks', title: 'Tasks' },
  { id: 'task', path: '/tickets/:id', title: 'Ticket' },
  { id: 'activity', path: '/activity', title: 'Activity' },
  { id: 'event', path: '/activity/events/:id', title: 'Activity' },
  { id: 'my-activity', path: '/my-activity', title: 'My activity' },
  { id: 'knowledge', path: '/knowledge', title: 'Knowledge' },
  { id: 'entry', path: '/knowledge/:id', title: 'Knowledge' },
  { id: 'schedules', path: '/schedules', title: 'Schedules' },
  { id: 'schedule', path: '/schedules/:id', title: 'Schedule' },
  { id: 'team-overview', path: '/team-overview', title: 'Team overview' },
  { id: 'stats', path: '/stats', title: 'Stats' },
  { id: 'notifications', path: '/notifications', title: 'Notifications' },
  { id: 'settings', path: '/settings', title: 'Settings' },
  { id: 'integrations', path: '/integrations', title: 'Integrations' },
  { id: 'personal-codex', path: '/personal-codex', title: 'Personal Codex' },
  { id: 'search', path: '/search', title: 'Search' },
  { id: 'about', path: '/about', title: 'About' },
]

const number = '[1-9][0-9]{0,18}'
const ticketKey = '[A-Z][A-Z0-9]{0,7}-' + number
const taskFragment = new RegExp(`^task-((?:[A-Z][A-Z0-9]{0,7}-)?${number})$`)
const eventFragment = new RegExp(`^event-(${number})-log$`)
const scheduleFragment = new RegExp(`^schedule-(R-${number})$`)
const entryKey = new RegExp(`^${ticketKey}$`)
const namedRoutes = new Map(boardRoutes.filter(route => !route.path.includes(':')).map(route => [route.id, route.path]))
const teamAnchors = new Map([
  ['now-title', 'now-title'], ['decisions-title', 'decisions-title'], ['outcomes-title', 'outcomes-title'],
  ['map', 'team-overview'], ['workstreams-title', 'team-overview'],
])

function makeLocation(pathname, params, anchor) {
  return { pathname, search: params.size ? '?' + params : '', hash: anchor ? '#' + anchor : '' }
}

// Published links use both named fragments and query-only fragments. The router
// uses page paths, with anchors reserved for sections within the matched page.
export function legacyLocation(fragment) {
  const [name, query] = fragment.replace(/^#/, '').split(/\?(.*)/s, 2)
  const queryOnly = query === undefined && name.includes('=')
  const params = new URLSearchParams(queryOnly ? name : query)
  const owner = params.get('dri')
  if (owner) {
    params.delete('dri')
    const section = params.get('section')
    params.delete('section')
    return makeLocation('/reports/agents/' + encodeURIComponent(owner), params, section)
  }
  if (params.get('search')?.trim()) {
    params.set('q', params.get('search'))
    params.delete('search')
    return makeLocation('/search', params)
  }

  const task = taskFragment.exec(name)
  if (task) return makeLocation('/tickets/' + task[1], params)
  const event = eventFragment.exec(name)
  if (event) return makeLocation('/activity/events/' + event[1], params)
  const schedule = scheduleFragment.exec(name)
  if (schedule) return makeLocation('/schedules/' + schedule[1], params)

  if (name === 'knowledge' && entryKey.test(params.get('entry') || '')) {
    const id = params.get('entry')
    params.delete('entry')
    return makeLocation('/knowledge/' + id, params)
  }
  if (name === 'reports-all') {
    params.set('scope', 'all')
    return makeLocation('/reports', params)
  }
  if (name === 'notifications-needs-me' || name === 'notifications-log') {
    params.set('view', name === 'notifications-log' ? 'log' : 'needs-me')
    return makeLocation('/notifications', params)
  }
  if (name === 'manage') {
    params.set('section', 'permissions')
    return makeLocation('/settings', params)
  }
  if (name === 'work-log-title') return makeLocation('/activity', params, name)
  if (teamAnchors.has(name)) return makeLocation('/team-overview', params, teamAnchors.get(name))
  if (params.has('schedules')) {
    params.delete('schedules')
    return makeLocation('/schedules', params)
  }
  if (queryOnly || name === 'board' || name === 'queue-heading') {
    const section = params.get('section')
    const anchor = section || (name === 'queue-heading' ? name : '')
    if (anchor) params.delete('section')
    return makeLocation(section === 'team-overview' ? '/team-overview' : '/tasks', params, anchor)
  }
  if (name === '' || namedRoutes.has(name)) return makeLocation(namedRoutes.get(name) || '/', params)
  return null
}

function namedFragment(name, params) {
  return params.size ? `${name}?${params}` : name
}

// Page components keep their existing filter grammar while their route is
// matched by React Router. Synthetic fragments are never written to history.
export function pageFragment(route, { search, hash }, { id, owner } = {}) {
  const params = new URLSearchParams(search)
  const anchor = hash?.slice(1)
  switch (route) {
    case 'home':
      return params.size ? namedFragment('reports', params) : ''
    case 'reports':
      if (params.get('scope') !== 'all' || params.get('view') === 'goal-activity') return namedFragment(route, params)
      params.delete('scope')
      return namedFragment('reports-all', params)
    case 'assignee':
      params.set('dri', owner)
      if (anchor) params.set('section', anchor)
      return params.toString()
    case 'task':
      return namedFragment('task-' + id, params)
    case 'event':
      return namedFragment('event-' + id + '-log', params)
    case 'entry': {
      params.set('entry', id)
      return namedFragment('knowledge', params)
    }
    case 'schedule':
      return namedFragment('schedule-' + id, params)
    case 'schedules':
      params.set('schedules', '')
      return params.toString()
    case 'search':
      return new URLSearchParams({ search: params.get('q') || '' }).toString()
    case 'notifications': {
      const view = params.get('view')
      params.delete('view')
      return namedFragment(view === 'needs-me' ? 'notifications-needs-me' : view === 'log' ? 'notifications-log' : route, params)
    }
    case 'team-overview':
      return namedFragment(teamAnchors.has(anchor) ? anchor : route, params)
    case 'board':
      if (anchor === 'queue-heading') params.set('section', anchor)
      return namedFragment(route, params)
    default:
      return namedFragment(route, params)
  }
}

export function routeAddress({ pathname, search, hash }) {
  return pathname + search + hash
}
