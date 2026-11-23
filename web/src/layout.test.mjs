import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

let vite
let App
let WorkQueue
let TaskDetail
let Assignee
let notificationLogFilters
let NotificationLogEntry
before(async () => {
  vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, hmr: false, ws: false, watch: null },
  })
  const app = await vite.ssrLoadModule('/src/App.jsx')
  App = app.default
  const work = await vite.ssrLoadModule('/src/Work.jsx')
  WorkQueue = work.WorkQueue
  TaskDetail = work.TaskDetail
  ;({ Assignee, notificationLogFilters } = await vite.ssrLoadModule('/src/shared.jsx'))
  ;({ NotificationLogEntry } = await vite.ssrLoadModule('/src/NotificationLog.jsx'))
})
after(() => vite?.close())

const teamSections = ['now-title', 'decisions-title', 'outcomes-title']

function render(fragment) {
  const previous = { window: globalThis.window, document: globalThis.document }
  try {
    globalThis.window = { location: { hash: fragment }, matchMedia: () => ({ matches: false }) }
    globalThis.document = { documentElement: { dataset: {} } }
    return renderToStaticMarkup(React.createElement(App))
  } finally {
    Object.assign(globalThis, previous)
  }
}

test('board routes share one keyboard-focusable main scroll region beneath the header', () => {
  for (const fragment of ['', '#board', '#task-COURSE-1', '#team-overview', '#activity', '#settings', '#stats']) {
    const markup = render(fragment)
    assert.equal((markup.match(/<main\b/g) || []).length, 1, fragment)
    assert.match(markup, /<\/header><main tabindex="0" aria-label="Taskboard content"><div class="main-content">/)
  }
})

test('Reports links to Team overview without embedding the board-wide feed', () => {
  const reports = render('')
  assert.match(reports, /id="reports"[^>]*>Reports/)
  assert.match(reports, /href="#team-overview">Team overview/)
  assert.doesNotMatch(reports, /id="queue-heading"|class="section-index"/)

  for (const fragment of ['team-overview', ...teamSections, 'map', 'workstreams-title']) {
    const overview = render(`#${fragment}`)
    assert.match(overview, /id="team-overview"[^>]*>Team overview/)
    assert.match(overview, /href="#reports" aria-current="page">Reports/)
    assert.match(overview, /class="section-index"/)
    assert.doesNotMatch(overview, /id="queue-heading"|href="#queue-heading"|class="task-detail"/)
    for (const id of teamSections) assert.ok(overview.includes(`id="${id}"`))
    assert.doesNotMatch(overview, /Dependency map|Workstreams|href="#workstreams-title"/)
  }
})

test('the combined Reports link opens the Reports tab', () => {
  const combined = render('#reports-all')
  assert.match(combined, /href="#reports-all" aria-current="page">Reports/)
  assert.match(combined, /<h1 id="reports">Reports/)
  assert.match(combined, /role="status">Loading Reports…/)
  assert.doesNotMatch(combined, /id="queue-heading"/)
})

test('Tasks and a directly linked ticket have separate main content', () => {
  for (const fragment of ['#board', '#queue-heading']) {
    const tasks = render(fragment)
    assert.match(tasks, /<h1 id="board">Tasks/)
    assert.match(tasks, /<h2 id="queue-heading">Work queue/)
    assert.doesNotMatch(tasks, /id="now-title"|id="outcomes-title"|class="section-index"/)
  }
  const ticket = render('#task-42')
  assert.match(ticket, /<article id="task-42"/)
  assert.match(ticket, /<h1 id="task-heading-42">Task T-42/)
  assert.match(ticket, /href="#board">Back to Tasks/)
  assert.match(ticket, /href="#board" aria-current="page">Tasks/)
  assert.doesNotMatch(ticket, /class="workspace"|id="queue-heading"|id="now-title"|id="outcomes-title"/)
})

test('page and section headings remain fragment targets without becoming focusable', () => {
  for (const fragment of ['#reports', '#board', '#queue-heading', '#team-overview', '#workstreams-title',
    '#activity', '#stats', '#notifications', '#notifications-needs-me', '#notifications-log', '#settings', '#dri=Avery', '#search=fixture', '#schedules=']) {
    const markup = render(fragment)
    assert.doesNotMatch(markup, /<h[1-6]\b[^>]*\btabindex=/, fragment)
    assert.doesNotMatch(markup, /<header id="assignee-title"[^>]*\btabindex=/, fragment)
  }
})

test('project ticket links open separate detail and return to the matching queue', () => {
  const ticket = render('#task-COURSE-1')
  assert.match(ticket, /<article id="task-COURSE-1"/)
  assert.match(ticket, /<h1 id="task-heading-COURSE-1">Task COURSE-1/)
  assert.match(ticket, /href="#project=COURSE">Back to Tasks/)
  assert.doesNotMatch(ticket, /id="queue-heading"|id="now-title"/)
  assert.match(render('#project=COURSE'), /id="queue-heading"[^>]*>Work queue/)
})

test('a search result ticket returns to the exact query and keeps its project ticket route', () => {
  const query = 'Inventory & taxes 50%?'
  const ticket = render(`#task-COURSE-1?${new URLSearchParams({ 'from-search': query })}`)
  assert.match(ticket, /<article id="task-COURSE-1"/)
  const back = ticket.match(/href="([^"]+)">Back to Search results/)?.[1]
  assert.equal(new URLSearchParams(back.slice(1)).get('search'), query)
  assert.doesNotMatch(ticket, /Back to Tasks/)
  assert.match(render(back), /<h1 id="search-results">Results for/)
})

test('ticket owner links preserve the project in full and compact views', () => {
  const task = { id: 42, key: 'COURSE-1', dri: 'Avery' }
  for (const compact of [false, true]) {
    const markup = renderToStaticMarkup(React.createElement(Assignee, { task, compact }))
    assert.match(markup, /href="#dri=Avery&amp;project=COURSE">Avery/)
  }
  const legacy = renderToStaticMarkup(React.createElement(Assignee, { task: { id: 42, dri: 'Avery' } }))
  assert.match(legacy, /href="#dri=Avery&amp;project=T">Avery/)
})

test('a Taskboard ticket returns to Taskboard from a course queue', () => {
  const ticket = renderToStaticMarkup(React.createElement(TaskDetail, {
    id: 42, preferredProject: 'COURSE', session: {},
  }))
  const backLink = ticket.match(/href="([^"]+)">Back to Tasks/)?.[1]
  assert.equal(backLink, '#project=T')
  assert.match(render(backLink), /id="queue-heading"[^>]*>Work queue/)
})

test('Settings remains a separate route alongside the layout views', () => {
  const settings = render('#settings')
  assert.match(settings, /id="settings"[^>]*>Settings/)
  assert.doesNotMatch(settings, /id="queue-heading"|id="now-title"|class="task-detail"/)
})

test('About has its own route and loads the running release', () => {
  const about = render('#about')
  assert.match(about, /id="about">About<\/h1>/)
  assert.match(about, /role="status">Loading release information…/)
  assert.doesNotMatch(about, /id="queue-heading"|github\.com\/skyzh\/taskboard\/commit\//)
})

test('a filtered Project log has the Notifications route', () => {
  const agent = '00000000-0000-0000-0000-000000000028'
  for (const kind of ['ready', 'wake']) {
    const fragment = `notifications-log?project=T&status=pending&kind=${kind}&agent=${agent}`
    assert.deepEqual(notificationLogFilters(fragment), { project: 'T', state: 'pending', kind, agent })
    const projectLog = render(`#${fragment}`)
    assert.match(projectLog, /id="notifications">Notifications<\/h1>/)
    assert.doesNotMatch(projectLog, /id="queue-heading"/)
  }
})

test('wake evidence keeps request acknowledgment separate from delivery and Start', () => {
  const at = '2026-11-23T02:00:17Z'
  const item = {
    id: 42, task_id: 20, task: 'T-20', kind: 'wake', recipient: 'Avery',
    cue: 'A Ready ticket was assigned to you.', acceptance: 'accepted', acceptance_at: at,
    delivery: 'unknown', next_result: 'no_work', next_at: at,
  }
  const renderWake = fields => renderToStaticMarkup(React.createElement(NotificationLogEntry, { item: { ...item, ...fields } }))
  const accepted = renderWake({})
  assert.match(accepted, /A Ready ticket was assigned to you\./)
  assert.match(accepted, /href="#event-42-log">Source activity<\/a>/)
  assert.match(accepted, /Raft acceptance<\/dt><dd><span>Accepted<\/span><time [^>]*>[^<]*:00:17/)
  assert.match(accepted, /Raft delivery<\/dt><dd><span>Unknown<\/span><\/dd>/)
  assert.match(accepted, /First signed queue check<\/dt><dd><span>No eligible work<\/span><time /)
  assert.match(accepted, /<dt>Start<\/dt><dd><span>Not observed<\/span><\/dd>/)
  assert.doesNotMatch(accepted, /Start activity|Delivered|Acknowledgment activity/)

  const acknowledged = renderWake({ request_acknowledgment: { id: 53, at } })
  assert.match(acknowledged, /Request acknowledgment<\/dt><dd><span>Acknowledged<\/span><time /)
  assert.match(acknowledged, /href="#event-53-log">Acknowledgment activity<\/a>/)
  assert.match(acknowledged, /<dt>Start<\/dt><dd><span>Not observed<\/span><\/dd>/)
  assert.match(acknowledged, /Raft delivery<\/dt><dd><span>Unknown<\/span><\/dd>/)

  const started = renderWake({
    delivery: 'delivered', delivery_at: at, next_result: 'offered', started_at: at, start_event_id: 55,
  })
  assert.match(started, /Raft delivery<\/dt><dd><span>Delivered<\/span><time /)
  assert.match(started, /First signed queue check<\/dt><dd><span>This ticket offered<\/span><time /)
  assert.match(started, /<dt>Start<\/dt><dd><span>Started<\/span><time /)
  assert.match(started, /href="#event-55-log">Start activity<\/a>/)
})

test('a restored queue renders the selected filters and the friendly waiting label', () => {
  const queue = renderToStaticMarkup(React.createElement(WorkQueue, {
    filters: { state: 'waiting', dri: 'Avery', waiting_on: 'raft:human:fixture' },
    waitingLabel: 'Legal team',
  }))
  assert.match(queue, /value="waiting" selected=""/)
  assert.match(queue, /value="owner:Avery" selected=""/)
  assert.match(queue, /list="dependencies" value="Legal team"/)
})

test('the Work queue shows the Merge queue with or without legacy deployment data', () => {
  const taskboard = renderToStaticMarkup(React.createElement(WorkQueue, {
    project: 'T', overview: { merge_queue: true, release_queue: {} }, filters: { state: 'ready' },
  }))
  assert.match(taskboard, /<summary>Merge queue<\/summary>/)
  assert.doesNotMatch(taskboard, /<summary>Deploy queue<\/summary>/)
  assert.match(taskboard, /value="ready" selected=""/)

  const course = renderToStaticMarkup(React.createElement(WorkQueue, {
    project: 'COURSE', overview: { release_queue: {} }, filters: {},
  }))
  assert.doesNotMatch(course, /<summary>(Merge|Deploy) queue<\/summary>/)
})
