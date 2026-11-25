import { createTimeZoneFormatters } from '../../src/timeZone.js'
import workflow from './workflow.json' with { type: 'json' }

const repository = 'https://github.com/skyzh/taskboard'
const pull = repository + '/pull/234'
const original = pull + '/commits/a987a54a79822d38d42539d2ccf7b1632c645976'
const fix = pull + '/commits/6d089198668ddb6cc9f6e7d8f1070e7f2f706e02'
const mainChecks = repository + '/actions/runs/45337609367'
const merge = '0b50c6ba1956f260bad6c1c0e88bd2bf458fa878'
const project = { prefix: 'T', title: 'Taskboard' }
export const profiles = {
  prism: { avatar_url: new URL('./avatars/prism.svg', import.meta.url).href, id: 'prism', name: 'Prism', kind: 'agent', dri_kind: 'team', role: 'Taskboard frontend' },
  gauge: { avatar_url: new URL('./avatars/gauge.svg', import.meta.url).href, id: 'gauge', name: 'Gauge', kind: 'agent', dri_kind: 'team', role: 'Independent reviewer' },
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
const blocker = event(2, 'gauge', 'log', 'Review requested a fix', '2026-11-24T05:55:25Z',
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
const workflowEvents = new Map(workflow.activity.map(event => [event.id, event]))
const combinedActivity = [...events, ...workflow.activity].sort(
  (first, second) => Date.parse(second.at) - Date.parse(first.at) || second.id - first.id,
)
const workflowRecords = new Map(Object.entries(workflow.records).flatMap(([key, record]) => [
  [key, record],
  [String(record.detail.task.id), record],
]))

function workflowReports(view) {
  if (view !== 'active' && view !== 'idle') return workflow.reports
  return workflow.reports.filter(entry => (entry.task_count > 0) === (view === 'active'))
}

export function previewRequest(path, method) {
  if (method.toUpperCase() !== 'GET') {
    throw Object.assign(new Error('This action requires the full board.'), { status: 403 })
  }
  const [endpoint, query] = path.split('?')
  const filters = new URLSearchParams(query)
  const owner = filters.get('owner')
  const projectFilter = filters.get('project')
  const allProjects = filters.get('scope') === 'all'
  const workflowProject = projectFilter === workflow.project.prefix
  const text = filters.get('q')?.toLowerCase()
  const matches = item => !text || `${item.key} ${item.title}`.toLowerCase().includes(text)
  const selectedTasks = allProjects ? [task, ...workflow.tasks] : workflowProject ? workflow.tasks : [task]
  const selectedEvents = allProjects ? combinedActivity : workflowProject
    ? workflow.activity : events
  const matchingEvents = selectedEvents.filter(item =>
    (!filters.has('actor') || item.actor === filters.get('actor')) &&
    (!filters.has('action') || item.action === filters.get('action')) &&
    (item.id > released.id || filters.get('detail') === 'all' || [released, blocker, approved].includes(item)))
  if (endpoint === '/session') return { actor: reader }
  if (endpoint === '/projects') return page([project, workflow.project])
  if (endpoint === '/revision') return { revision: 'public-demo-234-and-workflow' }
  if (endpoint === '/reports') return page(workflowProject ? workflowReports(filters.get('view')) : reports)
  if (endpoint === '/reports/request-log') return page(workflowProject ? workflow.request_log : [request])
  if (endpoint === '/projects/T/links') return { links: [{ kind: 'github', url: repository }] }
  if (endpoint === '/overview') {
    if (workflowProject) return workflow.overview
    const statusCounts = { ...counts, done: 1 }
    return { now: page(), decisions: page(), owner_profiles: profiles, counts: statusCounts,
      status_counts: statusCounts }
  }
  if (endpoint === '/assignee') {
    if (workflowProject) {
      const entry = workflow.reports.find(item => item.owner === owner)
      return { ...entry, has_outcomes: Boolean(entry?.recent_outcomes.length) }
    }
    return { profile: profiles[owner], task_counts: counts, pending_reports: [], has_outcomes: owner === 'prism' }
  }
  if (endpoint === '/tasks') {
    const state = filters.get('state')
    return page(selectedTasks.filter(item =>
      (!filters.has('dri') || filters.get('dri') === item.dri) &&
      (!state || (item.display_state || item.state) === state) &&
      (!filters.has('waiting_on') || filters.get('waiting_on') === item.waiting_on) &&
      (filters.get('hide_requests') !== 'true' || !item.peer_request) &&
      matches(item)))
  }
  if (/^\/tasks\/(?:1406|T-1406)$/.test(endpoint)) return detail
  if (/^\/tasks\/(?:1406|T-1406)\/activity$/.test(endpoint)) return page(events)
  if (endpoint.endsWith('/watch')) return { watching: false }
  const recordPath = endpoint.match(/^\/tasks\/([^/]+)(?:\/(activity|history)\/?(\d+)?)?$/)
  const record = workflowRecords.get(recordPath?.[1])
  if (record) {
    if (recordPath[2] === 'activity') return page(record.events.map(id => workflowEvents.get(id)))
    if (recordPath[2] === 'history' && record.history[recordPath[3]]) return record.history[recordPath[3]]
    if (!recordPath[2]) return { ...record.detail, can_edit_details: false, can_edit_checklist: false }
  }
  if (endpoint === '/activity') return page(matchingEvents)
  if (endpoint.startsWith('/activity/')) {
    const id = Number(endpoint.split('/').at(-1))
    return { event: workflowEvents.get(id) || events.find(item => item.id === id) }
  }
  if (endpoint === '/outcomes' || endpoint === '/outcomes/days') {
    const { formatDay } = createTimeZoneFormatters(filters.get('time_zone') || 'America/Los_Angeles')
    const outcomes = selectedEvents.filter(item => item.action === 'done' &&
      (!filters.has('dri') || filters.get('dri') === item.actor))
    if (endpoint === '/outcomes/days') {
      const outcomeDates = outcomes.map(item => formatDay(item.at))
      const result = { days: [...new Set(outcomeDates)].sort().reverse(), next_day: null }
      const weekEnding = filters.get('week_ending')
      if (weekEnding) {
        const lastDay = Date.parse(weekEnding + 'T00:00:00Z')
        result.week = Array.from({ length: 7 }, (_, index) => {
          const day = new Date(lastDay - (6 - index) * 86_400_000).toISOString().slice(0, 10)
          return { day, count: outcomeDates.filter(date => date === day).length }
        })
      }
      return result
    }
    return page(outcomes.filter(item => !filters.has('day') || formatDay(item.at) === filters.get('day')))
  }
  if (endpoint === '/knowledge') {
    return page([reportNote, ...workflow.knowledge].filter(item => matches(item) &&
      (!filters.has('entry') || filters.get('entry') === item.key) &&
      (!filters.has('source') || item.sources.some(source => source.task_key === filters.get('source')) ||
        item.staged_work?.some(change => change.source === filters.get('source')))))
  }
  if (/^\/tasks\/(?:234|T-234)$/.test(endpoint)) {
    const { id, key, title, summary } = reportNote
    return { task: { id, key, title, record_kind: 'knowledge' }, brief: summary, checklist: [], sources, related_tasks: [] }
  }
  if (/^\/tasks\/(?:234|T-234)\/activity$/.test(endpoint)) return page()
  if (endpoint === '/knowledge/areas') return { areas: workflow.areas }
  if (endpoint === '/release') return { commit: merge }
  if (endpoint === '/personal-agents/access') return { available: false }
  if (endpoint === '/personal-agents/local-dris') return { local_dris: [] }
  if (endpoint === '/personal-agents/publishing-instructions') return { instructions: '', can_save: false }
  if (endpoint === '/notifications/open-count') return { count: workflow.decisions.length }
  if (endpoint === '/notifications/decisions') {
    return page(filters.get('state') === 'answered' ? [] : workflow.decisions)
  }
  if (endpoint === '/notifications/needs-you') return page(workflowProject || allProjects ? workflow.needs_you : [])
  if (endpoint === '/notifications/work') return { items: [] }
  if (endpoint === '/notifications/preferences') return { task_creations: false }
  if (endpoint === '/stats' || endpoint.startsWith('/tasks/') || endpoint.includes('/history')) {
    throw Object.assign(new Error('This view requires the full board.'), { status: 404 })
  }
  return page()
}
