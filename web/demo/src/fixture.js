const repository = 'https://github.com/skyzh/taskboard'
const pull = repository + '/pull/234'
const original = pull + '/commits/a987a54a79822d38d42539d2ccf7b1632c645976'
const fix = pull + '/commits/6d089198668ddb6cc9f6e7d8f1070e7f2f706e02'
const mainChecks = repository + '/actions/runs/45337609367'
const merge = '0b50c6ba1956f260bad6c1c0e88bd2bf458fa878'
const project = { prefix: 'T', title: 'Taskboard' }
const profiles = {
  prism: { id: 'prism', name: 'Prism', kind: 'agent', dri_kind: 'team', role: 'Taskboard frontend' },
  gauge: { id: 'gauge', name: 'Gauge', kind: 'agent', dri_kind: 'team', role: 'Independent reviewer' },
}
const reader = { id: 'demo-reader', name: 'Demo reader', kind: 'human' }

export const capturedAt = '2026-11-24T06:06:11Z'

const task = {
  id: 1406, key: 'T-1406', record_kind: 'work', title: 'Open All projects from the Reports home URL',
  state: 'done', dri: 'prism', dri_profile: profiles.prism, priority: 'normal', summary_label: 'Outcome',
  note: 'The home URL now opens Reports with All projects selected. Explicit project links and single-project accounts retain their layout.',
  checklist_total: 0, checklist_completed: 0,
}

function event(id, actor, action, label, at, note, source, linkLabel) {
  return {
    id, task_id: task.id, task_key: task.key, task_title: task.title,
    actor, actor_profile: profiles[actor], action, label, at, note,
    links: [{ url: source, label: linkLabel }],
  }
}

const opened = event(1, 'prism', 'add', 'Opened for review', '2026-11-24T05:55:25Z',
  'Prism opened PR #234 with the default Reports change and handed it to Gauge for independent review.',
  original, 'Original revision')
// The review preceded the fix. The replay uses the linked revisions as time bounds for the review record.
const blocker = event(2, 'gauge', 'blocked', 'Review requested a fix', '2026-11-24T05:55:25Z',
  'The bare-URL test expected the old landing state. Gauge asked Prism to cover loading and the All projects landing before approval.',
  original, 'Revision that needed a fix')
const corrected = event(3, 'prism', 'update', 'Test fixed', '2026-11-24T05:59:33Z',
  'Prism separated the explicit Reports link check from the bare home URL check and verified the All projects loading state.',
  fix, 'Read the test fix')
const approved = event(4, 'gauge', 'update', 'Review approved', '2026-11-24T05:59:33Z',
  'Gauge approved the fixed revision after the checks passed, returning it to Prism to ship.',
  pull, 'Review and checks')
const released = event(5, 'prism', 'done', 'Released', capturedAt,
  'PR #234 merged and the protected main checks passed.',
  mainChecks, 'Main checks')
const events = [released, approved, corrected, blocker, opened]
const counts = { ready: 0, in_progress: 0, parked: 0, human_review: 0 }
const reports = Object.entries(profiles).map(([owner, profile]) => ({
  owner, profile, last_activity: events.find(item => item.actor === owner),
  tasks: [], task_count: 0, task_counts: counts, pending_reports: [],
  recent_outcomes: owner === 'prism' ? [released] : [],
}))
const request = {
  id: '234-review', phase: 'done', at: opened.at,
  requester: 'prism', requester_profile: profiles.prism, recipient: 'gauge', recipient_profile: profiles.gauge,
  source_id: task.id, source_key: task.key, source_title: task.title, pull_urls: [pull], response: approved,
}
const sources = [
  { label: 'Pull request #234', url: pull },
  { label: 'Review fix', url: fix },
  { label: 'Protected main checks', url: mainChecks },
]
const reportNote = {
  id: 234, key: 'T-234', title: 'Demo note on verifying the Reports home view', area: 'Reports',
  summary: 'Use the bare home URL to verify All projects. Use explicit links to verify a project selection and the Reports navigation.',
  sources: [{ task_key: task.key, event_id: released.id, at: released.at, label: 'Verified release' }],
}
const detail = {
  task, brief: 'Show All projects by default on the Reports home page. Preserve explicit project links, reload and Back behavior, and the single-project layout.',
  checklist: [], sources, related_tasks: [], workers: [], last_activity: released,
}
const page = (items = []) => ({ items, next_cursor: null })

export function previewRequest(path, method) {
  if (method.toUpperCase() !== 'GET') {
    throw Object.assign(new Error('This action requires the full board.'), { status: 403 })
  }
  const [endpoint, query] = path.split('?')
  const filters = new URLSearchParams(query)
  const owner = filters.get('owner')
  const text = filters.get('q')?.toLowerCase()
  const matches = item => !text || `${item.key} ${item.title}`.toLowerCase().includes(text)
  const matchingEvents = events.filter(item =>
    (!filters.has('actor') || item.actor === filters.get('actor')) &&
    (!filters.has('action') || item.action === filters.get('action')) &&
    (filters.get('detail') === 'all' || [released, blocker, approved].includes(item)))
  if (endpoint === '/session') return { actor: reader }
  if (endpoint === '/projects') return page([project])
  if (endpoint === '/revision') return { revision: 'public-demo-234' }
  if (endpoint === '/reports') return page(reports)
  if (endpoint === '/reports/request-log') return page([request])
  if (endpoint === '/projects/T/links') return { links: [{ service: 'github', url: repository }] }
  if (endpoint === '/overview') return { now: page(), decisions: page(), owner_profiles: profiles, status_counts: { ...counts, done: 1 } }
  if (endpoint === '/assignee') return { profile: profiles[owner], task_counts: counts, pending_reports: [], has_outcomes: owner === 'prism' }
  if (endpoint === '/tasks') {
    const fits = (!filters.has('dri') || filters.get('dri') === task.dri) &&
      (!filters.has('state') || filters.get('state') === task.state) && matches(task)
    return page(fits ? [task] : [])
  }
  if (/^\/tasks\/(?:1406|T-1406)$/.test(endpoint)) return detail
  if (/^\/tasks\/(?:1406|T-1406)\/activity$/.test(endpoint)) return page(events)
  if (endpoint.endsWith('/watch')) return { watching: false }
  if (endpoint === '/activity') return page(matchingEvents)
  if (endpoint.startsWith('/activity/')) return { event: events.find(item => item.id === Number(endpoint.split('/').at(-1))) }
  if (endpoint === '/outcomes/days') return { days: filters.get('dri') === 'gauge' ? [] : ['2026-11-24'], next_day: null }
  if (endpoint === '/outcomes') return page(filters.get('dri') === 'gauge' ? [] : [released])
  if (endpoint === '/knowledge') {
    return page(matches(reportNote) && (!filters.has('entry') || filters.get('entry') === reportNote.key) &&
      (!filters.has('source') || filters.get('source') === task.key) ? [reportNote] : [])
  }
  if (/^\/tasks\/(?:234|T-234)$/.test(endpoint)) {
    const { id, key, title, summary } = reportNote
    return { task: { id, key, title, record_kind: 'knowledge' }, brief: summary, checklist: [], sources, related_tasks: [] }
  }
  if (/^\/tasks\/(?:234|T-234)\/activity$/.test(endpoint)) return page()
  if (endpoint === '/knowledge/areas') return { areas: [] }
  if (endpoint === '/release') return { commit: merge }
  if (endpoint === '/personal-agents/access') return { available: false }
  if (endpoint === '/personal-agents/local-dris') return { local_dris: [] }
  if (endpoint === '/personal-agents/publishing-instructions') return { instructions: '', can_save: false }
  if (endpoint === '/notifications/open-count') return { count: 0 }
  if (endpoint === '/notifications/work') return { items: [] }
  if (endpoint === '/notifications/preferences') return { task_creations: false }
  if (endpoint === '/stats' || endpoint.startsWith('/tasks/') || endpoint.includes('/history')) {
    throw Object.assign(new Error('This view requires the full board.'), { status: 404 })
  }
  return page()
}
