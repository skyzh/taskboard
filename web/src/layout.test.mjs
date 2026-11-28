import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

let vite
let App
let BoardViews
let WorkQueue
let TaskDetail
let Assignee
let notificationLogFilters
let NotificationLogEntry
let ResourceStatus
let BoardOutageContext
before(async () => {
  vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, hmr: false, ws: false, watch: null },
  })
  const app = await vite.ssrLoadModule('/src/App.jsx')
  App = app.default
  BoardViews = (await vite.ssrLoadModule('/src/BoardViews.jsx')).default
  const work = await vite.ssrLoadModule('/src/Work.jsx')
  WorkQueue = work.WorkQueue
  TaskDetail = work.TaskDetail
  ;({ Assignee, notificationLogFilters, ResourceStatus } = await vite.ssrLoadModule('/src/shared.jsx'))
  ;({ NotificationLogEntry } = await vite.ssrLoadModule('/src/NotificationLog.jsx'))
  ;({ BoardOutageContext } = await vite.ssrLoadModule('/src/BoardOutage.jsx'))
})
after(() => vite?.close())

const teamSections = ['now-title', 'decisions-title', 'outcomes-title']

function render(fragment, { welcomeHidden, ...props } = {}) {
  const previous = { window: globalThis.window, document: globalThis.document }
  try {
    globalThis.window = {
      location: { hash: fragment }, matchMedia: () => ({ matches: false }),
      localStorage: { getItem: key => key === 'tasks-board.reports-welcome-hidden' ? welcomeHidden : null },
    }
    globalThis.document = { documentElement: { dataset: {} } }
    return renderToStaticMarkup(React.createElement(App, props))
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

test('a board outage reserves space for missing reads and keeps specific errors actionable', () => {
  const unavailable = { status: 502, message: 'Upstream failed' }
  const renderStatus = (resource, outage = true) => renderToStaticMarkup(
    React.createElement(BoardOutageContext.Provider, { value: outage },
      React.createElement(ResourceStatus, { resource, label: 'task activity' })),
  )

  const placeholder = renderStatus({ error: unavailable })
  assert.match(placeholder, /class="read-placeholder"/)
  assert.match(placeholder, /task activity could not be loaded/)
  assert.doesNotMatch(placeholder, /Upstream failed|<button/)
  assert.equal(renderStatus({ error: unavailable, data: { items: [] } }), '')
  assert.match(renderStatus({ error: unavailable }, false), /Upstream failed.*<button/)
  assert.match(renderStatus({ error: { status: 403, message: 'Sign in as the ticket owner' } }),
    /Sign in as the ticket owner.*<button/)
})

test('Reports links to Team overview without embedding the board-wide feed', () => {
  const reports = render('#reports')
  assert.match(reports, /id="reports"[^>]*>Reports/)
  assert.match(reports, /href="#team-overview">Team overview/)
  assert.doesNotMatch(reports, /id="queue-heading"|class="section-index"/)

  for (const fragment of ['team-overview', ...teamSections, 'map', 'workstreams-title']) {
    const overview = render(`#${fragment}`)
    assert.match(overview, /id="team-overview"[^>]*>Team overview/)
    assert.match(overview, /href="#reports" aria-current="page">Reports/)
    assert.match(overview, /class="section-index"/)
    assert.doesNotMatch(overview, /id="queue-heading"|href="#queue-heading"|class="task-detail"/)
    for (const id of teamSections) assert.ok(overview.includes(`<h2 id="${id}" tabindex="-1">`))
    assert.doesNotMatch(overview, /Dependency map|Workstreams|href="#workstreams-title"/)
  }
})

test('the direct Reports tour waits for the configured project', () => {
  assert.match(render('#reports'), /class="reports-tour-trigger" disabled="">Take the tour/)
})

test('the demo starts with the full welcome banner even when a real-board dismissal is saved', () => {
  const welcomeHidden = 'true'
  assert.doesNotMatch(render('#reports', { welcomeHidden }), /reports-welcome-title/)
  const demo = render('#reports', { welcomeHidden, previewNotice: 'Fictional demo' })
  assert.match(demo, /<h2 id="reports-welcome-title">Get started with Taskboard<\/h2>/)
  assert.match(demo, /Taskboard is a shared workspace/)
  assert.match(demo, /aria-label="Dismiss welcome banner"/)
  assert.doesNotMatch(demo, /reports-welcome-toggle/)
})

test('the home URL and combined Reports link wait for the project list', () => {
  for (const fragment of ['', '#reports-all']) {
    const reports = render(fragment)
    assert.match(reports, /href="#reports-all" aria-current="page">Reports/)
    assert.match(reports, /<h1 id="reports">Reports/)
    assert.match(reports, /role="status">Loading Reports…/)
    assert.doesNotMatch(reports, /id="queue-heading"|href="#team-overview"/)
  }
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

test('only Team overview section headings can receive programmatic focus', () => {
  for (const fragment of ['#reports', '#board', '#queue-heading', '#team-overview', '#workstreams-title',
    '#activity', '#stats', '#notifications', '#notifications-needs-me', '#notifications-log', '#settings', '#dri=Avery', '#search=fixture', '#schedules=']) {
    const markup = render(fragment)
    const otherHeadings = markup.replace(/<h2 id="(?:now-title|decisions-title|outcomes-title)" tabindex="-1">/g, '')
    assert.doesNotMatch(otherHeadings, /<h[1-6]\b[^>]*\btabindex=/, fragment)
    assert.doesNotMatch(markup, /<header id="assignee-title"[^>]*\btabindex=/, fragment)
  }
})

test('project ticket links open separate detail and return to the matching queue', () => {
  const ticket = render('#task-COURSE-1')
  assert.match(ticket, /<article id="task-COURSE-1"/)
  assert.match(ticket, /<h1 id="task-heading-COURSE-1">Task COURSE-1/)
  assert.match(ticket, /href="#project=COURSE">Back to Tasks/)
  assert.doesNotMatch(ticket, /id="queue-heading"|id="now-title"/)
  assert.match(render('#project=COURSE'), /Loading projects…/)
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
  assert.match(render(backLink), /Loading projects…/)
})

test('an invalid decision link stays unavailable instead of opening the current ticket form', () => {
  for (const pin of ['decision-request=', 'decision-request=oops', 'decision-request=12&decision-request=13']) {
    const markup = render(`#task-T-7?${pin}`)
    assert.match(markup, /This decision request is unavailable\./)
    assert.doesNotMatch(markup, /Record decision/)
  }
  assert.doesNotMatch(render('#task-T-7'), /This decision request is unavailable/)
  assert.doesNotMatch(render('#task-T-7?decision-request=12'), /This decision request is unavailable/)
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

test('the dashboard Notifications tab opens Inbox for all viewers', () => {
  for (const [identity, route] of [
    [{ isHuman: true }, 'notifications'],
    [{ isAgent: true }, 'notifications'],
    [{}, 'notifications'],
  ]) {
    for (const [projectScope, query] of [['COURSE', 'project=COURSE'], ['all', 'scope=all']]) {
      const markup = renderToStaticMarkup(React.createElement(BoardViews, {
        ...identity, current: 'notifications', reportsHref: '#reports', projectScope, showNotifications: true,
      }))
      assert.ok(markup.includes(`href="#${route}?${query}" aria-current="page">Notifications</a>`))
    }
  }
})

test('a filtered Project log has the Notifications route', () => {
  const agent = '00000000-0000-0000-0000-000000000028'
  for (const kind of ['ready', 'wake']) {
    const fragment = `notifications-log?project=T&status=pending&kind=${kind}&agent=${agent}`
    assert.deepEqual(notificationLogFilters(fragment), { project: 'T', state: 'pending', kind, agent })
    const projectLog = render(`#${fragment}`)
    assert.match(projectLog, /Loading projects…/)
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
  assert.match(accepted, /Dispatch<\/dt><dd><span>Accepted<\/span><time [^>]*>[^<]*:00:17/)
  assert.match(accepted, /Delivery method not recorded/)
  assert.match(accepted, /First signed queue check<\/dt><dd><span>No eligible work<\/span><time /)
  assert.doesNotMatch(accepted, /<dt>(Start|Delivery)<|Start activity|Delivered|Acknowledgment activity/)

  const acknowledged = renderWake({ request_acknowledgment: { id: 53, at } })
  assert.match(acknowledged, /Request acknowledgment<\/dt><dd><span>Acknowledged<\/span><time /)
  assert.match(acknowledged, /href="#event-53-log">Acknowledgment activity<\/a>/)
  assert.doesNotMatch(acknowledged, /<dt>(Start|Delivery)<|Not observed/)

  const started = renderWake({
    carrier: 'raft_app', delivery: 'delivered', delivery_at: at, next_result: 'offered', started_at: at, start_event_id: 55,
  })
  assert.match(started, /Raft app event/)
  assert.match(started, /Delivery<\/dt><dd><span>Delivered<\/span><time /)
  assert.match(started, /First signed queue check<\/dt><dd><span>This ticket offered<\/span><time /)
  assert.match(started, /<dt>Start<\/dt><dd><span>Started<\/span><time /)
  assert.match(started, /href="#event-55-log">Start activity<\/a>/)
  assert.match(renderWake({ carrier: 'local_command', next_result: 'different_work', started_at: at }), /Other work started/)
  assert.match(renderWake({ carrier: 'raft_reminder' }), /Raft reminder/)
  assert.match(renderWake({ acceptance: 'unknown', next_result: null }), /Dispatch unconfirmed/)

  const goal = renderWake({
    reason: 'goal_review', carrier: 'raft_app', cue: null, next_result: null, goal_outcome: { id: 57, at },
  })
  assert.match(goal, /Scheduled goal review/)
  assert.match(goal, /Signed outcome recorded/)
  assert.match(goal, /<dt>Dispatch<\/dt><dd><span>Accepted<\/span>/)
  assert.match(goal, /href="#event-57-log">Outcome activity<\/a>/)
  assert.doesNotMatch(goal, /Source activity|<dt>(Start|Delivery)<|Picked up/)
  const unsendable = renderWake({ reason: 'goal_explore', acceptance: 'unsendable', next_result: null })
  assert.match(unsendable, /Cue could not be prepared/)
  assert.doesNotMatch(unsendable, /Raft app event|<dt>Dispatch<|Delivery method not recorded/)
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
