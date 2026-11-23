import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { queryPath } from './data.js'

const require = createRequire(import.meta.url)
const fixture = { resources: {}, pages: {}, state: [], queryPath }
globalThis[Symbol.for('tasks.activityControls.fixture')] = fixture
const fixtureLookup = "globalThis[Symbol.for('tasks.activityControls.fixture')]"

let WorkLog
let WorkQueue
let TaskDetail
let DecisionForm
let TaskClosure
let TaskDetailsEdit
let TaskOwnership
let TaskTags
let ReminderDate
let PendingReports
let Reports
let AssigneePage
let EventRow
let OutcomeRow
let Outcomes
let TimeZoneContext
let createTimeZoneFormatters
let Notifications
let GoalActivity
let DriGoal
let DriMilestones
let TaskRow
let AttentionRequests
let Stats
let QueuedAsks
let MyActivity
before(async () => {
  const result = await build({
    stdin: {
      contents: `
        export { WorkLog, WorkQueue, EventRow, TaskDetail } from './Work.jsx'
        export { default as DecisionForm } from './DecisionForm.jsx'
        export { default as TaskClosure } from './TaskClosure.jsx'
        export { default as TaskDetailsEdit } from './TaskDetailsEdit.jsx'
        export { default as TaskOwnership } from './TaskOwnership.jsx'
        export { default as TaskTags } from './TaskTags.jsx'
        export { default as ReminderDate } from './ReminderDate.jsx'
        export { default as Reports, PendingReports } from './Reports.jsx'
        export { default as AssigneePage } from './AssigneePage.jsx'
        export { OutcomeRow, Outcomes } from './Overview.jsx'
        export { TimeZoneContext, createTimeZoneFormatters } from './timeZone.js'
        export { default as Notifications } from './Notifications.jsx'
        export { default as GoalActivity } from './GoalActivity.jsx'
        export { default as DriGoal, DriMilestones } from './DriGoal.jsx'
        export { TaskRow } from './WorkRow.jsx'
        export { default as AttentionRequests } from './AttentionRequests.jsx'
        export { default as Stats } from './Stats.jsx'
        export { default as QueuedAsks } from './QueuedAsks.jsx'
        export { default as MyActivity } from './MyActivity.jsx'
      `,
      resolveDir: fileURLToPath(new URL('.', import.meta.url)),
    },
    bundle: true,
    format: 'cjs',
    platform: 'node',
    external: ['lucide-react'],
    loader: { '.css': 'empty' },
    write: false,
    plugins: [{
      name: 'activity-controls-fixture',
      setup(plugin) {
        plugin.onResolve({ filter: /^(react|\.\/data\.js)$/ }, ({ path }) => ({
          path, namespace: 'fixture',
        }))
        plugin.onLoad({ filter: /.*/, namespace: 'fixture' }, ({ path }) => ({
          contents: path === 'react' ? `
            import { useEffect as reactEffect, useId as reactId, useRef as reactRef } from ${JSON.stringify(require.resolve('react'))}
            export { default, createContext, useContext, useLayoutEffect, useMemo } from ${JSON.stringify(require.resolve('react'))}
            export function useCallback(callback) { return callback }
            export function useId() { return ${fixtureLookup}.directIds?.shift() ?? reactId() }
            export function useEffect(effect, deps) {
              const capture = ${fixtureLookup}.captureEffect
              if (capture) capture(effect)
              else reactEffect(effect, deps)
            }
            export function useRef(initial) {
              const refs = ${fixtureLookup}.refs
              return refs ? refs.shift() : reactRef(initial)
            }
            export function useState(initial) {
              const fixture = ${fixtureLookup}
              return [fixture.state.length ? fixture.state.shift() : initial, value => fixture.onSetState?.(value)]
            }
          ` : `
            export function useResource(path) {
              const fixture = ${fixtureLookup}
              fixture.resourceReads?.push(path)
              return fixture.resources[path] || {}
            }
            export function useLoadedPages(path, filters, _revision, options) {
              const page = ${fixtureLookup}.pages[path]
              return typeof page === 'function' ? page(filters, options) : page || { data: options?.firstPage }
            }
            export const queryPath = (path, params) => ${fixtureLookup}.queryPath(path, params)
            export function request(...args) {
              const send = ${fixtureLookup}.request
              if (!send) throw new Error('Unexpected request')
              return send(...args)
            }
          `,
        }))
        plugin.onResolve({ filter: /node_modules\/react\/index\.js$/ }, ({ path }) => ({ path, external: true }))
      },
    }],
  })
  const module = { exports: {} }
  new Function('require', 'module', result.outputFiles[0].text)(require, module)
  ;({
    WorkLog, WorkQueue, TaskDetail, DecisionForm, TaskClosure, TaskDetailsEdit, TaskOwnership, TaskTags,
    ReminderDate, PendingReports, Reports, AssigneePage, EventRow, OutcomeRow, Outcomes,
    TimeZoneContext, createTimeZoneFormatters,
    Notifications, GoalActivity, DriGoal, DriMilestones, TaskRow, AttentionRequests, Stats, QueuedAsks, MyActivity,
  } = module.exports)
})

test('an assignee returns to the current Reports scope', () => {
  const props = { owner: 'Sample owner', project: 'DEMO' }
  const path = queryPath('/assignee', props)
  fixture.resources[path] = { data: { pending_reports: [], has_outcomes: false, task_counts: {
    ready: 0, in_progress: 0, waiting: 0, human_review: 0, parked: 0,
  } } }
  try {
    const combined = renderToStaticMarkup(React.createElement(AssigneePage, { ...props, reportsHref: '#reports-all' }))
    assert.match(combined, /href="#reports-all">Back to Reports/)
    const direct = renderToStaticMarkup(React.createElement(AssigneePage, props))
    assert.match(direct, /href="#reports">Back to Reports/)
  } finally {
    delete fixture.resources[path]
  }
})

test('Needs you reads only the selected project on the signed-in human’s own report', () => {
  const owner = 'raft:human:sample'
  const project = 'COURSE'
  const props = { owner, project, revision: 3 }
  const path = queryPath('/assignee', { owner, project })
  const attentionPath = '/notifications/needs-you'
  fixture.resources[path] = { data: { profile: { id: owner, name: 'Sam' }, pending_reports: [], has_outcomes: false } }
  const scopes = []
  fixture.pages[attentionPath] = filters => {
    scopes.push(filters)
    return { data: { items: [{ id: 'decision:1', ticket: 'COURSE-1', title: 'Choose a date', kind: 'decision' }] } }
  }
  const render = viewer => renderToStaticMarkup(React.createElement(AssigneePage, { ...props, viewer }))

  try {
    const otherHuman = { id: 'raft:human:other', name: 'Sam', kind: 'human' }
    const ownAgent = { id: owner, name: 'Sam', kind: 'agent' }
    for (const viewer of [null, otherHuman, ownAgent]) {
      assert.doesNotMatch(render(viewer), /Needs you|Choose a date/)
    }
    assert.deepEqual(scopes, [])

    const ownReport = render({ id: owner, name: 'Sam', kind: 'human' })
    assert.match(ownReport, /Needs you/)
    assert.match(ownReport, /href="#task-COURSE-1".*Choose a date/)
    assert.deepEqual(scopes, [{ project }])
  } finally {
    delete fixture.resources[path]
    delete fixture.pages[attentionPath]
  }
})

test('task reminders show the selected local time and reopen the exact instant in the board time zone', () => {
  const context = { defer_at: '2030-04-01T12:30:00-07:00', defer_until: '2030-04-01T19:30:00Z' }
  const task = { id: 1, title: 'Resume the export', state: 'parked', dri: 'Avery', context }
  const row = renderToStaticMarkup(React.createElement(TaskRow, { task }))
  assert.match(row, /Deferred until <time dateTime="2030-04-01T19:30:00Z">2030-04-01 12:30:00-07:00<\/time>/)

  const exact = renderToStaticMarkup(React.createElement(TimeZoneContext.Provider, {
    value: createTimeZoneFormatters('Asia/Shanghai'),
  }, React.createElement(ReminderDate, { path: '/tasks/1/reminder', exactTime: true, savedUntil: context.defer_until })))
  assert.match(exact, /<option value="time" selected="">Exact time<\/option>/)
  assert.match(exact, /Defer until \(Asia\/Shanghai\)/)
  assert.match(exact, /value="2030-04-02T03:30"/)
  assert.match(exact, /Clear reminder/)

  const dateOnly = renderToStaticMarkup(React.createElement(ReminderDate, {
    path: '/notifications/requests/1/reminder', saved: '2030-04-01',
  }))
  assert.match(dateOnly, /Date \(Pacific time\)/)
  assert.doesNotMatch(dateOnly, /Exact time|datetime-local/)
})

test('only the owner can park ordinary work, and a parked task needs a return path', () => {
  const task = { id: 1, key: 'T-1', event_id: 1, title: 'Check the export', state: 'ready',
    summary_label: 'Next step', dri: 'Avery', note: 'Inspect the export.', links: [] }
  const session = { data: { actor: { id: 'Avery', kind: 'agent' } } }
  function detail(changes, goal, viewer = session) {
    fixture.resources = { '/tasks/1': { data: {
      task: { ...task, ...changes }, checklist: [], workers: [], sources: [], goal,
    } } }
    fixture.pages = {}
    fixture.state = []
    return renderToStaticMarkup(React.createElement(TaskDetail, { id: 1, session: viewer }))
  }

  const ready = detail({})
  assert.match(ready, /<summary>Park work<\/summary>/)
  assert.match(ready, /use Waiting on/)
  assert.match(ready, /Dependency review/)
  assert.match(ready, /type="submit" disabled="">Park task<\/button>/)
  assert.doesNotMatch(detail({}, null, {}), /<summary>Park work<\/summary>/)

  const parked = { state: 'parked' }
  const legacy = detail(parked)
  assert.match(legacy, /<summary>Add a return path<\/summary>/)
  assert.doesNotMatch(legacy, /Keep current return path|Clear reminder/)
  const context = { review_after: '2030-04-01' }
  const scheduled = detail({ ...parked, context })
  assert.match(scheduled, /Keep current return path/)
  assert.doesNotMatch(scheduled, /Clear reminder/)
  const readiness = { source_id: 2, source_key: 'T-2', criteria: 'Check the updated export.' }
  const redundant = detail({ ...parked, context: { ...context, readiness } })
  assert.match(redundant, /Review after <a href="#task-2"><span class="ticket-id">T-2<\/span> <\/a> changes/)
  assert.match(redundant, /Clear reminder/)

  const goal = detail(parked, { kind: 'standing', tickets: [] })
  assert.match(goal, /<summary>Set a reminder<\/summary>/)
  assert.doesNotMatch(goal, /Return to Inbox|Add a return path/)
})

test('Stats keeps the agent name in the row header beside a photo or initials', () => {
  const from = '2026-11-21T05:00:00Z'
  const to = '2026-11-21T06:00:00Z'
  const photo = 'https://tasks.example.test/tasks/api/v1/avatars/fixture/digest'
  fixture.resources = {
    '/stats?period=day': { data: {
      from, to, bucket_seconds: 3600, buckets: [{ from, to, dris: 1, workers: 0 }],
      activity: {
        events: 7, completions: 2, tasknote_events: 0, tasks: [], task_count: 0, agent_count: 2, other_events: 0,
        agents: [
          { name: 'Portrait Agent', avatar_url: photo, events: 4, completions: 2 },
          { name: 'Missing Photo', events: 3, completions: 0 },
        ],
      },
      worker_hours: { elapsed_seconds: 0, measured_runs: 0, unclosed_runs: 0, invalid_runs: 0 },
    } },
  }
  fixture.state = []
  const markup = renderToStaticMarkup(React.createElement(Stats))
  assert.match(markup, /<th scope="row"><span class="assignee stats-agent"><img class="assignee-avatar" src="[^"]+" width="20" height="20" alt=""\/><span>Portrait Agent<\/span><\/span><\/th><td>4<\/td><td>2<\/td>/)
  assert.ok(markup.includes(photo))
  assert.match(markup, /<span class="assignee-avatar initials" aria-hidden="true">MP<\/span><span>Missing Photo<\/span>/)

  fixture.state = [photo]
  const failed = renderToStaticMarkup(React.createElement(Stats))
  assert.match(failed, /<span class="assignee-avatar initials" aria-hidden="true">PA<\/span><span>Portrait Agent<\/span>/)
  assert.ok(!failed.includes(photo))
})

const selected = {
  id: 18,
  task_id: 1,
  task_title: 'Check the CSV',
  actor: 'Morgan',
  at: '2026-11-21T05:00:00Z',
  action: 'log',
  label: 'Activity logged',
  note: 'The header was fixed.',
  headline: 'The header was fixed.',
  links: [],
}

test('Stats uses current states and keeps window and agent completion totals', () => {
  const from = '2026-11-20T05:00:00Z'
  const to = '2026-11-21T05:00:00Z'
  fixture.state = []
  fixture.resources = {
    '/stats?period=day': { data: {
      from, to, bucket_seconds: 3600, buckets: [{ from, to, dris: 1, workers: 1 }],
      worker_hours: { elapsed_seconds: 0, measured_runs: 0, unclosed_runs: 0, invalid_runs: 0 },
      activity: {
        events: 9, completions: 2, tasknote_events: 1,
        tasks: [
          { id: 1, key: 'T-1', title: 'Historical completion', state: 'done', events: 4, completions: 0, has_tasknote: 1, tasknote_events: 1 },
          { id: 2, key: 'DEMO-1', title: 'Document the caveats', state: 'human_review', events: 3, completions: 2 },
          { id: 3, title: 'Legacy snapshot', events: 1, completions: 0 },
          { id: 4, key: 'DEMO-2', title: 'An abandoned duplicate', state: 'done', display_state: 'closed',
            events: 1, completions: 0 },
        ],
        task_count: 4,
        agents: [{ name: 'Avery Agent', events: 5, completions: 2 }],
        agent_count: 1, other_events: 4,
      },
    } },
  }
  const zone = createTimeZoneFormatters('America/Los_Angeles')
  const markup = renderToStaticMarkup(React.createElement(TimeZoneContext.Provider, { value: zone }, React.createElement(Stats)))
  assert.match(markup, /datetime="2026-11-20T05:00:00Z">Nov 19, 2026.*9:00\s*PM PST<\/time>/i)
  assert.match(markup, /<dt>Activity events<\/dt><dd>9<\/dd>/)
  assert.match(markup, /<dt>Recorded Done<\/dt><dd>2<\/dd>/)
  assert.match(markup, /<th scope="col">Status<\/th>/)
  assert.match(markup, /1 DRI, 1 worker"/)
  assert.match(markup, /4 events came from other actors or sources\./)
  assert.match(markup, /href="#task-1".*?Historical completion<\/a><\/th><td>4<\/td><td><span class="status done">Done<\/span>/)
  assert.match(markup, /href="#task-DEMO-1"><span class="ticket-id">DEMO-1<\/span> Document the caveats<\/a>/)
  assert.match(markup, /href="#task-3"><span class="ticket-id">T-3<\/span> Legacy snapshot<\/a>/)
  assert.match(markup, /class="status human_review">Assigned to human<\/span>/)
  assert.match(markup, /class="status closed">Closed<\/span>/)
  assert.match(markup, /<td>Unavailable<\/td>/)
  assert.match(markup, /<span>Avery Agent<\/span><\/span><\/th><td>5<\/td><td>2<\/td>/)
  assert.doesNotMatch(markup, /TaskNote|Note events/i)
  assert.doesNotMatch(markup, />Refresh<\/button>/)
  assert.doesNotMatch(markup, />Retry<\/button>/)

  fixture.resources = { '/stats?period=day': { error: new Error('Temporary failure.') } }
  const failed = renderToStaticMarkup(React.createElement(Stats))
  assert.match(failed, /Unable to load stats. Temporary failure./)
  assert.match(failed, />Retry<\/button>/)
})

test('Stats shows watcher coverage, leaves missing baseline values blank, and displays measured zero', () => {
  const from = '2026-11-21T05:00:00Z'
  const to = '2026-11-22T05:00:00Z'
  fixture.state = []
  fixture.resources = {
    '/stats?period=day': { data: {
      from, to, bucket_seconds: 3600, buckets: [{ from, to, dris: 0, workers: 0 }],
      activity: { events: 0, completions: 0, tasks: [], agents: [], task_count: 0, agent_count: 0, other_events: 0 },
      worker_hours: { elapsed_seconds: 0, measured_runs: 0, unclosed_runs: 0, invalid_runs: 0 },
      watcher_measurements: { cohorts: [
        { key: 'baseline', label: 'Bounded baseline', run_count: 5, metrics: {
          ready_at_first_idle: { mean: null, measured_runs: 0 },
          wake_to_start_seconds: { mean: 14.5, measured_runs: 2 },
        } },
        { key: 'repeat', label: 'Repeat watcher', run_count: 2, metrics: {
          ready_at_first_idle: { mean: 4, measured_runs: 2 },
          working_seconds: { mean: 73.449, measured_runs: 1 },
          completed_tasks: { mean: 0, measured_runs: 1 },
        } },
      ] },
    } },
  }
  const markup = renderToStaticMarkup(React.createElement(Stats))
  assert.match(markup, /<th scope="col">Bounded baseline<\/th><th scope="col">Repeat watcher<\/th>/)
  assert.match(markup, /<th scope="row">Observed runs<\/th><td>5<\/td><td>2<\/td>/)
  assert.match(markup, /<th scope="row">Runs with a Ready count<\/th><td>0<\/td><td>2<\/td>/)
  assert.match(markup, /<th scope="row">Ready at first idle sample<\/th><td><\/td><td>4/)
  assert.match(markup, /<th scope="row">Wake to recorded Start \(s\)<\/th><td>14\.5/)
  assert.match(markup, /<th scope="row">Sampled working time \(s\)<\/th><td><\/td><td>73\.4/)
  assert.match(markup, /<th scope="row">Tasks completed<\/th><td><\/td><td>0<div class="stats-note">1 measured<\/div>/)
})

test('Activity and project outcomes agree on the local day without changing UTC timestamps or More', () => {
  const event = { ...selected, action: 'done', label: 'Completed', task_key: 'COURSE-1', at: '2026-11-21T07:59:59Z' }
  for (const [zone, day, clock] of [
    ['America/Los_Angeles', '2026-11-20', /11:59\s*PM PST/],
    ['UTC', '2026-11-21', /7:59\s*AM UTC/],
  ]) {
    fixture.state = []
    fixture.resources = {}
    fixture.pages = {
      '/activity': { data: { items: [event] } },
      '/outcomes/days': (filters, options) => {
        assert.deepEqual(filters, { dri: 'Morgan', project: 'COURSE', time_zone: zone })
        assert.deepEqual([options.limit, options.moreLimit, options.cursorParam], [2, 1, 'before_day'])
        return { data: { items: [day], next_cursor: day }, loadMore() {} }
      },
      '/outcomes': filters => {
        assert.deepEqual(filters, { dri: 'Morgan', day, project: 'COURSE', time_zone: zone })
        return { data: { items: [event] } }
      },
    }
    const markup = renderToStaticMarkup(React.createElement(TimeZoneContext.Provider, {
      value: createTimeZoneFormatters(zone),
    }, React.createElement(WorkLog), React.createElement(Outcomes, { owner: 'Morgan', project: 'COURSE' })))
    assert.equal((markup.match(new RegExp(`datetime="${day}"`, 'gi')) || []).length, 2)
    assert.match(markup, clock)
    assert.match(markup, /datetime="2026-11-21T07:59:59Z"/i)
    assert.match(markup, /href="#task-COURSE-1"/)
    assert.match(markup, /aria-label="More days of outcomes"/)
  }
})

function renderLog(items, linked, fragment = 'event-18-log') {
  fixture.pages = { '/activity': { data: { items } } }
  fixture.resources = { '/activity/18': linked }
  fixture.state = []
  return renderToStaticMarkup(React.createElement(WorkLog, { requested: 18, fragment }))
}

test('the directly linked event stays visible when filters return no rows', () => {
  const markup = renderLog([], { data: selected }, 'event-18-log?actor=Taylor&action=done')
  assert.match(markup, /Linked event/)
  assert.match(markup, /id="event-18-log"/)
  assert.match(markup, /The header was fixed\./)
  assert.match(markup, /Clear filters/)
  assert.match(markup, /value="Taylor"/)
  assert.match(markup, /value="done" selected/)
  assert.doesNotMatch(markup, /No matching activity/)
})

test('task rows distinguish the current wait, decision and Ready states', t => {
  t.mock.method(Date, 'now', () => Date.parse('2026-11-21T08:00:00Z'))
  fixture.state = []
  const since = '2026-11-21T05:00:00Z'
  const task = { id: 1, title: 'Review the report', note: '', priority: 'high', at: since }
  const render = state => renderToStaticMarkup(React.createElement(TaskRow, { task: { ...task, ...state }, owner: false }))

  const blocked = render({ state: 'waiting', waiting_since: since, blockers: { tickets: [{ id: 2 }] } })
  assert.match(blocked, /dateTime="2026-11-21T05:00:00Z"/i)
  assert.match(blocked, />Blocked for 3 hours<\/time>/)
  assert.match(render({ state: 'waiting', waiting_since: since }), />Waiting for 3 hours<\/time>/)
  assert.match(render({ state: 'human_review', action: 'redirect-decision' }), />Unanswered for 3 hours<\/time>/)
  assert.doesNotMatch(render({ state: 'ready', waiting_since: since }), /(?:Waiting|Blocked|Unanswered) for 3 hours/)
  assert.doesNotMatch(render({ state: 'waiting', waiting_since: '2026-11-21' }), /<time/)
})

test('a deferred request keeps its original age and a handled request has no active age', t => {
  t.mock.method(Date, 'now', () => Date.parse('2026-11-21T08:00:00Z'))
  const request = { id: 4, task_id: 1, sender: 'Avery', reason: 'Review it.',
    raised_at: '2026-11-20T07:00:00Z', remind_on: '2026-11-22', links: [] }
  const zone = createTimeZoneFormatters('America/Los_Angeles')
  const render = (state, item) => {
    fixture.state = [state]
    fixture.pages = { '/notifications/requests': { data: { items: [item] } } }
    return renderToStaticMarkup(React.createElement(TimeZoneContext.Provider, { value: zone },
      React.createElement(AttentionRequests)))
  }

  const deferred = render('deferred', request)
  assert.match(deferred, /datetime="2026-11-20T07:00:00Z" title="Nov 19, 2026.*11:00\s*PM PST"/i)
  assert.match(deferred, />Unanswered for 1 day<\/time>/)
  assert.match(deferred, /2026-11-22/)
  assert.doesNotMatch(render('handled', { ...request, handled_at: '2026-11-21T06:00:00Z' }), /Unanswered/)
})

test('attention cards link a separate blocked ticket and report only a recorded Ready outcome', () => {
  const task = { id: 9, key: 'COURSE-3', title: 'Verify the release', ready_event_id: null }
  const item = { id: 8, task_id: 1, task_key: 'T-1', task_title: 'Ask for signoff', sender: 'Avery',
    reason: 'Review the build.', raised_at: '2026-11-21T07:00:00Z', links: [], unblocks: task }
  fixture.state = []
  fixture.resources = {}
  fixture.pages = { '/notifications/requests': { data: { items: [item] } } }
  const needsMe = renderToStaticMarkup(React.createElement(Notifications, { session: {}, verified: true, fragment: 'notifications-needs-me' }))
  assert.match(needsMe, /href="#task-1".*Ask for signoff/)
  assert.match(needsMe, /Unblocks <a href="#task-COURSE-3".*Verify the release/)
  assert.match(needsMe, /Mark handled/)
  assert.doesNotMatch(needsMe, /For your information/)
  const inbox = renderToStaticMarkup(React.createElement(Notifications, { session: {}, verified: true }))
  assert.doesNotMatch(inbox, /Ask for signoff|Mark handled/)

  const renderRequests = (state, request) => {
    fixture.state = [state]
    fixture.pages = { '/notifications/requests': { data: { items: [request] } } }
    return renderToStaticMarkup(React.createElement(AttentionRequests, state === 'open' ? { taskId: 1 } : {}))
  }
  assert.match(renderRequests('open', item), /Unblocks <a href="#task-COURSE-3".*Verify the release/)
  const informationalRequest = renderRequests('open', { ...item, unblocks: null })
  assert.match(informationalRequest, /For your information/)
  assert.doesNotMatch(informationalRequest, /Unblocks|View Ready event/)
  const handled = { ...item, handled_at: '2026-11-21T08:00:00Z' }
  assert.doesNotMatch(renderRequests('handled', handled), /Moved|Unblocks|View Ready event/)
  const completed = renderRequests('handled', { ...handled, unblocks: { ...task, ready_event_id: 24 } })
  assert.match(completed, /Moved <a href="#task-COURSE-3".* to Ready/)
  assert.match(completed, /href="#event-24-log">View Ready event<\/a>/)
})

test('ticket attention hides stale requests while refreshing and preserves the handled receipt', () => {
  const item = { id: 8, task_id: 1, sender: 'Avery', reason: 'Check the rollout.',
    raised_at: '2026-11-21T07:00:00Z', links: [] }
  const render = (page, state = []) => {
    fixture.state = [...state]
    fixture.pages = { '/notifications/requests': filters => {
      assert.deepEqual(filters, { state: 'open', task_id: 1, scope: 'all' })
      return page
    } }
    return renderToStaticMarkup(React.createElement(AttentionRequests, { taskId: 1 }))
  }

  assert.equal(render({ data: { items: [] } }), '')
  const open = render({ data: { items: [item] } })
  assert.match(open, /aria-label="Attention requests".*Check the rollout\./)
  assert.match(open, /Mark handled/)
  assert.doesNotMatch(open, /<select|<summary>Attention requests/)

  const receipt = { id: 8, handled_at: '2026-11-21T08:00:00Z', unblocked_task_id: 2,
    unblocked_task_title: 'Verify the change', ready_event_id: 24 }
  const stale = render({ data: { items: [item] }, error: Error('Refresh failed.') })
  assert.match(stale, />Retry<\/button>/)
  assert.doesNotMatch(stale, /Mark handled|Check the rollout\./)

  const cached = { items: [item], next_cursor: 2 }
  const invalidated = ['open', null, null, [], cached]
  const retrying = render({ data: cached, pending: true }, invalidated)
  assert.match(retrying, /Loading attention requests/)
  assert.doesNotMatch(retrying, /Mark handled|Check the rollout\.|>More</)

  const background = render({ data: cached, pending: true })
  assert.match(background, /Mark handled/)
  assert.doesNotMatch(background, /Loading attention requests/)

  const paging = render({ data: cached, pending: true, loadingMore: true })
  assert.match(paging, /Mark handled/)
  assert.match(paging, /Loading more attention requests/)

  const reloading = render({ data: cached, pending: true, loadingMore: true }, invalidated)
  assert.match(reloading, /Loading attention requests/)
  assert.doesNotMatch(reloading, /Mark handled|Loading more/)
  const reloadFailure = render({ data: cached, moreError: Error('Refresh failed.') }, invalidated)
  assert.match(reloadFailure, /Unable to load attention requests\. Refresh failed\./)
  assert.doesNotMatch(reloadFailure, /Mark handled|load more/)

  const after = render({ data: { items: [item] }, error: Error('Refresh failed.') },
    ['open', null, { receipt }, [item.id]])
  assert.match(after, /Unable to load attention requests\. Refresh failed\./)
  assert.match(after, />Retry<\/button>/)
  assert.match(after, /href="#event-24-log">View Ready event<\/a>/)
  assert.doesNotMatch(after, /Mark handled|Check the rollout\./)
})

test('Inbox keeps dated and standing goals after actionable work and every loaded page', () => {
  fixture.resources = {
    '/notifications/preferences': { data: { task_creations: true } },
    '/notifications/work?scope=all': { data: {
      items: [
        { kind: 'ready', id: 1, key: 'T-1', title: 'Owned task' },
        { kind: 'review', id: 2, key: 'T-2', title: 'Independent check' },
        { kind: 'shared_ready', id: 3, key: 'T-3', title: 'Shared task' },
      ],
      dated_goals: [
        { project: 'T', summary: 'Review the cutover', deadline: '2030-11-28', ticket: 'T-42' },
        { project: 'T', summary: 'Verify the import', deadline: '2030-12-03', ticket: 'T-43' },
      ],
      standing_goals: [{ project: 'COURSE', summary: 'Improve handoffs when the queue drains', ticket: 'COURSE-7' }],
    } },
  }
  const creation = (id, name) => ({ kind: 'creation', id, item: {
    id, task_id: id, task_key: `T-${id}`, task_title: name, at: '2026-11-21T07:00:00Z',
  } })
  const first = creation(4, 'A newer update')
  const second = creation(5, 'An earlier update')
  function render(items, nextCursor) {
    fixture.state = []
    fixture.pages = { '/notifications/inbox': {
      data: { items, next_cursor: nextCursor }, loadMore: nextCursor ? () => {} : null,
    } }
    const session = { data: { actor: { id: 'Avery', kind: 'agent' } } }
    return renderToStaticMarkup(React.createElement(Notifications, { session, verified: true }))
  }
  for (const [items, cursor] of [[[first], 'cursor'], [[first, second], null]]) {
    const markup = render(items, cursor)
    const ordered = ['Owned task', 'Independent check', 'Shared task', ...items.map(row => row.item.task_title),
      ...(cursor ? ['More inbox items'] : []), 'Review the cutover', 'Verify the import', 'Improve handoffs when the queue drains']
    let previous = -1
    for (const title of ordered) {
      const position = markup.indexOf(title)
      assert.ok(position > previous, title)
      previous = position
    }
    assert.match(markup, /aria-label="Dated goals".*href="#task-T-42".*Review the cutover.*<time dateTime="2030-11-28">2030-11-28<\/time>/)
    assert.match(markup, /href="#task-T-43".*Verify the import.*<time dateTime="2030-12-03">2030-12-03<\/time>/)
    const standing = markup.slice(markup.indexOf('aria-label="Standing goals"'))
    assert.match(standing, /aria-label="Standing goals".*href="#task-COURSE-7">Standing goal<\/a>/)
    assert.doesNotMatch(standing, /<time/)
  }
})

test('Inbox handles dated goals as its only content and accepts older work responses', () => {
  fixture.state = []
  fixture.pages = { '/notifications/inbox': { data: { items: [] } } }
  const session = { data: { actor: { id: 'Avery', kind: 'agent' } } }
  function render(work) {
    fixture.resources = {
      '/notifications/preferences': { data: { task_creations: true } },
      '/notifications/work?scope=all': { data: { items: [], ...work } },
    }
    return renderToStaticMarkup(React.createElement(Notifications, { session, verified: true }))
  }

  const datedOnly = render({
    dated_goals: [{ project: 'COURSE', summary: 'Review the draft', deadline: '2030-11-28', ticket: 'COURSE-7' }],
    standing_goals: [],
  })
  assert.match(datedOnly, /href="#task-COURSE-7".*Review the draft/)
  assert.doesNotMatch(datedOnly, /Your inbox is empty|aria-label="Next actionable work"/)

  const older = render({ standing_goals: [{ project: 'T', summary: 'Improve handoffs' }] })
  assert.match(older, /#dri=Avery&amp;section=dri-standing-goal-title&amp;project=T.*Improve handoffs/)
  assert.doesNotMatch(older, /Your inbox is empty|aria-label="Dated goals"/)
  assert.match(render({ standing_goals: [] }), /Your inbox is empty/)
})

test('Inbox shows empty only after its required reads settle successfully', () => {
  const empty = { data: { items: [] } }
  const failed = { error: Object.assign(Error('Try again later.'), { status: 503 }) }
  const missing = { error: Object.assign(Error('Not found.'), { status: 404 }) }
  const session = { data: { actor: { id: 'Avery', kind: 'agent' } } }
  function render(updates, work) {
    fixture.state = []
    fixture.pages = { '/notifications/inbox': updates }
    fixture.resources = {
      '/notifications/preferences': { data: { task_creations: true } },
      '/notifications/work?scope=all': work,
    }
    return renderToStaticMarkup(React.createElement(Notifications, { session, verified: true }))
  }

  assert.match(render(empty, empty), /Your inbox is empty/)
  const legacy = render(empty, missing)
  assert.match(legacy, /Your inbox is empty/)
  assert.doesNotMatch(legacy, /Unable to load/)

  assert.doesNotMatch(render(empty, { pending: true }), /Your inbox is empty/)
  assert.doesNotMatch(render({ ...empty, pending: true }, empty), /Your inbox is empty/)

  const workFailure = render(empty, failed)
  assert.match(workFailure, /Unable to load next actionable work\. Try again later\./)
  assert.match(workFailure, />Retry<\/button>/)
  assert.doesNotMatch(workFailure, /Your inbox is empty/)

  const updateFailure = render({ ...empty, ...failed }, empty)
  assert.match(updateFailure, /Unable to load your inbox\. Try again later\./)
  assert.doesNotMatch(updateFailure, /Your inbox is empty/)
})

test('Goal activity filters owners and goals, and reports offers only for standing goals', () => {
  const lastCheck = '2026-11-22T14:30:00Z'
  const standing = {
    key: 'T-42', owner: 'Avery', kind: 'standing', summary: 'Improve handoffs',
    starts: 2, completed_checks: 1, last_completed_at: lastCheck, idle_offers: null,
    outcomes: [{ id: 28, at: lastCheck, note: 'No issue found.' }],
  }
  const dated = {
    key: 'T-43', owner: 'Blake', kind: 'milestone', summary: 'Review the launch checklist',
    deadline: '2020-12-01', starts: 1, completed_checks: 1, last_completed_at: lastCheck,
    idle_offers: 0, outcomes: [{ id: 29, at: lastCheck, note: 'Checklist reviewed.' }],
  }
  fixture.resources = { '/reports/goal-activity?project=T': { data: { items: [standing, dated] } } }
  const render = (agent, goal) => renderToStaticMarkup(React.createElement(GoalActivity, { project: 'T', agent, goal }))
  const all = render()
  assert.match(all, /Standing-goal offers<\/dt><dd>Unmeasured/)
  assert.match(all, /The watcher records reminder requests/)
  assert.doesNotMatch(render(null, 'T-43'), /Standing-goal offers<\/dt>/)
  assert.match(all, /Completed checks<\/dt><dd>1/)
  assert.match(all, /href="#event-28-log"><time dateTime="2026-11-22T14:30:00Z"/)
  assert.match(all, /No issue found/)
  assert.match(all, /dateTime="2020-12-01"/)
  assert.doesNotMatch(all, /Overdue/)
  assert.doesNotMatch(render('Avery'), /href="#task-T-43"/)
  assert.doesNotMatch(render(null, 'T-43'), /href="#task-T-42"|href="#event-28-log"/)
  fixture.resources['/reports/goal-activity?project=T'].data.items[0] = { ...standing, idle_offers: 0 }
  assert.match(render(null, 'T-42'), /Standing-goal offers<\/dt><dd>0/)
})

test('Project log shows empty only after the current read succeeds', () => {
  const empty = { data: { items: [], agent_options: [], has_receipts: false } }
  const failed = { error: Object.assign(Error('Try again later.'), { status: 503 }) }
  const session = { data: { actor: { id: 'Sam', kind: 'human' } } }
  const message = /No notification receipts have been recorded for this project/
  function render(page) {
    fixture.state = []
    fixture.pages = { '/notifications/log': page }
    fixture.resources = {}
    return renderToStaticMarkup(React.createElement(Notifications, {
      session, verified: true, fragment: 'notifications-log?project=T',
    }))
  }

  assert.match(render(empty), message)
  assert.doesNotMatch(render({ ...empty, pending: true }), message)
  for (const page of [failed, { ...empty, ...failed }]) {
    const failure = render(page)
    assert.match(failure, /Unable to load project notifications\. Try again later\./)
    assert.match(failure, />Retry<\/button>/)
    assert.doesNotMatch(failure, message)
  }

  const items = [{ id: 17, kind: 'ready', recipient: 'Avery', task_id: 5, task: 'T-5', status: 'unknown' }]
  const cachedRows = render({ data: { items }, ...failed })
  assert.match(cachedRows, /Ready notice for Avery/)
  assert.match(cachedRows, />Retry<\/button>/)
})

test('profile renders two dated milestones and keeps the standing edit date-free', () => {
  const milestones = [
    { summary: 'Review the cutover', deadline: '2026-11-28', tickets: [{ id: 42, key: 'T-42', title: 'Cutover' }] },
    { summary: 'Verify the import', deadline: '2026-12-03', tickets: [{ id: 43, key: 'T-43', title: 'Import' }] },
  ]
  fixture.state = []
  const profile = renderToStaticMarkup(React.createElement(DriMilestones, { milestones, project: 'T', editable: true }))
  assert.match(profile, /Review the cutover.*2026-11-28.*href="#task-42".*Verify the import.*2026-12-03.*href="#task-43"/)
  assert.match(profile, /Add milestone/)
  fixture.state = ['Verify the import']
  const edit = renderToStaticMarkup(React.createElement(DriMilestones, { milestones, project: 'T', editable: true }))
  assert.match(edit, /Milestone name.*value="Verify the import".*Deadline.*value="2026-12-03".*value="T-43"/)
  fixture.state = [true]
  const standing = renderToStaticMarkup(React.createElement(DriGoal, { standing: true, project: 'T', editable: true }))
  assert.match(standing, /Standing goal.*Save goal/)
  assert.doesNotMatch(standing, /type="date"|Related task IDs/)
})

test('goal editors show the project title guidance and submit longer names intact', async () => {
  const project = 'COURSE'
  const title = 'Make the release evidence easy to find in every supported project view'
  const props = { goal: { summary: 'Improve evidence' }, standing: true, project, editable: true, onChanged() {} }
  fixture.resources = { '/projects/COURSE/field-guidance': { data: { fields: {
    title: { guidance: 'Describe the outcome.', where: 'Ticket headings and queue rows.', example: 'Verify the phone view.' },
  } } } }
  fixture.state = [true, title]
  const markup = renderToStaticMarkup(React.createElement(DriGoal, props))
  assert.match(markup, /<label>Standing goal<input[^>]+aria-describedby="([^"]+)"[^>]*\/><\/label>.*Describe the outcome\..*Ticket headings and queue rows\..*Verify the phone view\./)
  assert.match(markup, /<button>Save goal<\/button>/)

  const sent = []
  fixture.request = async (path, options) => sent.push({ path, ...options })
  try {
    fixture.state = [true]
    const editor = React.Children.toArray(DriGoal(props).props.children).find(child => child.props?.mode === 'standing-goal')
    fixture.state = [title, '', '', false, null]
    fixture.directIds = ['goal-links', 'goal-title-guidance']
    const form = editor.type(editor.props)
    form.props.onSubmit({ preventDefault() {} })
    assert.deepEqual(sent, [{ path: '/profile/standing-goal?project=COURSE', method: 'PATCH', body: { summary: title } }])
  } finally {
    delete fixture.directIds
    delete fixture.request
  }
})

test('an event already in the activity list has one anchor and is marked current', () => {
  const markup = renderLog([selected], null)
  assert.equal((markup.match(/id="event-18-log"/g) || []).length, 1)
  assert.match(markup, /aria-current="location"/)
  assert.match(markup, /Linked event/)
})

test('the empty activity message waits for the linked event request', () => {
  assert.doesNotMatch(renderLog([], { pending: true }), /No activity to show|No matching activity/)
  assert.match(renderLog([], null), /No activity to show/)
  assert.match(renderLog([], null, 'event-18-log?actor=Taylor'), /No matching activity/)
})

test('more reports disappears when the last report loads', () => {
  const initial = [{
    run_id: 1, task_id: 1, task_title: 'Check the CSV', worker: 'Morgan',
    report: 'Found an empty header.', links: [],
  }]
  const later = { ...initial[0], run_id: 2, report: 'Checked the corrected rows.' }
  const props = { owner: 'Avery', name: 'Avery', initial, total: 2, cursor: 8 }
  fixture.state = []
  fixture.pages = {}
  const firstPage = renderToStaticMarkup(React.createElement(PendingReports, props))
  assert.match(firstPage, /aria-label="More reports for Avery"[^>]*>More<\/button>/)
  assert.match(firstPage, /<details class="report-details"><summary aria-label="Worker report and evidence for W-1">/)
  assert.match(firstPage, /Found an empty header\./)

  fixture.pages = { '/reports/pending': {
    data: { items: [...initial, later], next_cursor: null },
  } }
  const markup = renderToStaticMarkup(React.createElement(PendingReports, props))
  assert.match(markup, /<details class="report-details"><summary aria-label="Worker report and evidence for W-2">/)
  assert.match(markup, /Checked the corrected rows\./)
  assert.match(markup, /Found an empty header\./)
  assert.doesNotMatch(markup, /aria-label="More reports for Avery"/)
})

test('project-scoped report pagination retains prefixed task links', () => {
  const first = {
    run_id: 1, task_id: 17, task_key: 'COURSE-1', task_title: 'Course sample', worker: 'Morgan',
    report: 'Initial feedback.', links: [],
  }
  fixture.state = [true]
  fixture.resources = {}
  fixture.pages = {
    '/reports/pending': (filters, options) => {
      assert.deepEqual(filters, { owner: 'Avery', project: 'COURSE' })
      assert.equal(options.limit, 10)
      assert.equal(options.firstPage.next_cursor, 8)
      return { data: { items: [first, { ...first, run_id: 2, report: 'Course details checked.' }], next_cursor: null } }
    },
  }
  const markup = renderToStaticMarkup(React.createElement(PendingReports, {
    owner: 'Avery', name: 'Avery', project: 'COURSE', initial: [first], total: 2, cursor: 8,
  }))
  assert.match(markup, /Course details checked\./)
  assert.match(markup, /href="#task-COURSE-1"/)
  assert.match(markup, /Initial feedback\./)
  assert.doesNotMatch(markup, /aria-label="More reports for Avery"/)
})

test('Activity and task history show only new sentences from a legacy receipt', () => {
  const headline = 'The reviewer checked all 12 sample exports across desktop and mobile layouts.'
  const details = 'Routed the findings to T-3, T-7, and T-12. The fixture covers rendered pages and expanded controls; native dialogs, external login, and transient errors were outside the sample.'
  const event = { ...selected, action: 'done', label: 'Completed', headline: undefined, note: headline + ' ' + details }
  for (const markup of [
    renderLog([event], null),
    renderToStaticMarkup(React.createElement(EventRow, { event })),
    renderToStaticMarkup(React.createElement(OutcomeRow, { events: [event] })),
  ]) {
    assert.equal(markup.split(headline).length - 1, 1)
    assert.match(markup, /<summary>Details<\/summary>/)
    assert.ok(markup.includes(details))
    assert.match(markup, /href="#event-18-log"/)
  }
})

test('typed provider and board work events use their distinct named marks in every Activity view', () => {
  const pull = [{ label: 'GitHub pull request', url: 'https://github.com/example/board/pull/17' }]
  const issue = [{ label: 'GitHub issue', url: 'https://github.com/example/board/issues/17' }]
  const github = (event_kind, links = pull, more = {}) => ({
    action: 'source-event', label: 'Source update', links,
    context: { source_kind: 'github', event_kind, ...more },
  })
  const cases = [
    [github('opened'), 'Pull request opened', 'git-pull-request'],
    [github('review-approved'), 'GitHub review', 'file-check'],
    [github('merged'), 'Pull request merged', 'git-merge'],
    [github('closed', pull, { pull_closed_without_merge: true }), 'Pull request closed without merge', 'git-pull-request-closed'],
    [github('closed'), 'Source update', 'arrow-up-right'],
    [github('opened', issue), 'Source update', 'arrow-up-right'],
    [{ action: 'start', label: 'Started' }, 'Started', 'play'],
    [{ action: 'decide', label: 'Decision recorded' }, 'Decision recorded', 'diamond'],
    [{ action: 'log', context: { work_kind: 'release-released' } }, 'Deployed', 'rocket'],
    [{ action: 'log', context: { work_kind: 'release-rolled_back' } }, 'Deployment rolled back', 'undo-2'],
    [{ action: 'log', context: { work_kind: 'local-merge' } }, 'Merged locally', 'git-merge'],
    [{ action: 'log', context: { stamp_review: { pull: 'example/board#17' } } }, 'Stamp review requested', 'file-check'],
    [{ action: 'transfer', context: { stamp_verdict: { verdict: 'approved' } } }, 'Stamp review', 'file-check'],
    [{ action: 'update', context: { peer_ask_ticket: 'T-3' } }, 'Peer request updated', 'messages-square'],
    [{ action: 'wait', label: 'Wait recorded', context: { peer_ask_ticket: 'T-3' } }, 'Wait recorded', 'clock'],
    [{ action: 'done', context: { peer_correction: { previous_close: 12 } } }, 'Peer result corrected', 'check'],
    [{ action: 'log', context: { audit: { action: 'checklist-complete' } } }, 'Checklist step completed', 'list-checks'],
    [{ action: 'log', context: { audit: { action: 'edit-task-details' } } }, 'Ticket details edited', 'pencil-line'],
    [{ action: 'log', label: 'Ticket reassigned', context: { ownership_action: 'reassign' } }, 'Ticket reassigned', 'corner-up-right'],
    [{ action: 'log', context: { extension: 'github-events', github_event: 'pull-merged' } }, 'Pull request merged', 'git-merge'],
    [{ action: 'log', context: { extension: 'github-events', github_event: 'deployed' } }, 'Deployed', 'rocket'],
    [{ action: 'done', label: 'Completed', context: { extension: 'github-events', github_event: 'deployed' } }, 'Completed', 'check'],
    [{ action: 'log', label: 'Activity logged', headline: 'Closed and deployed a fix.' }, 'Activity logged', 'message-square-text'],
    [{ action: 'update', label: 'Progress' }, 'Progress', 'pencil-line'],
    [{ action: 'done', label: 'Completed', headline: 'Fixed the CSV.' }, 'Completed', 'check'],
    [{ action: 'legacy', label: 'Legacy record' }, 'Legacy record', 'file-text'],
  ]
  for (const [fields, label, icon] of cases) {
    const event = { ...selected, ...fields }
    fixture.state = []
    const row = renderToStaticMarkup(React.createElement(EventRow, { event }))
    const ticket = renderToStaticMarkup(React.createElement(EventRow, { event, ticketActivity: true }))
    for (const markup of [row, ticket, renderLog([event], null), renderLog([event], null, 'event-18-log?detail=all')]) {
      assert.match(markup, new RegExp(
        `aria-label="${label}" title="${label}"><svg[^>]*class="lucide lucide-${icon}" aria-hidden="true" focusable="false"`,
      ), label)
    }
  }
})

test('a single line or exactly repeated explicit details need no disclosure', () => {
  for (const event of [
    { ...selected, headline: undefined },
    { ...selected, details: selected.headline },
  ]) {
    assert.doesNotMatch(renderLog([event], null), /<summary>Details/)
    const row = renderToStaticMarkup(React.createElement(EventRow, { event }))
    assert.doesNotMatch(row, /<summary>Details/)
    assert.match(row, /href="#event-18-log"/)
  }
})

test('Summary offers Active and All, including when the project has a configured roster', () => {
  function render(project, roster, selection, initialAssigneeView, expectedView) {
    fixture.state = [false, selection]
    fixture.pages = { '/reports': filters => {
      assert.deepEqual(filters, { project, view: expectedView })
      return { data: { items: [], next_cursor: null, ...(roster && { roster }) } }
    } }
    return renderToStaticMarkup(React.createElement(Reports, { project, initialAssigneeView }))
  }

  for (const [project, roster, selection, initial, expected] of [
    ['T', true, { project: 'COURSE', view: 'all' }, undefined, 'active'],
    ['T', true, { project: 'T', view: 'all' }, undefined, 'all'],
    ['COURSE', false, null, 'all', 'all'],
  ]) {
    const page = render(project, roster, selection, initial, expected)
    assert.match(page, /<select>/)
    assert.match(page, />Active<\/option>/)
    assert.match(page, />All<\/option>/)
    assert.doesNotMatch(page, /Other assignees/)
    assert.match(page, new RegExp(`value="${expected}" selected`))
    assert.match(page, /Team overview/)
  }
})

test('All projects keeps the same assignee and pending reports scoped to their own project', () => {
  const projects = [
    { prefix: 'T', title: 'Tasks Board' },
    { prefix: 'DEMO', title: 'Demo project' },
    { prefix: 'EMPTY', title: 'Empty project' },
  ]
  const report = (project, task, run) => ({
    owner: 'Avery', tasks: [{ id: task, key: `${project}-1`, title: 'Check the sample', state: 'ready', note: 'Read the sample.' }],
    task_count: 1, task_counts: { ready: 1 },
    pending_reports: [{ run_id: run, task_id: task, task_key: `${project}-1`, task_title: 'Check the sample', worker: 'Blake' }],
    pending_reports_before: run, report_count: 2,
  })
  const pendingScopes = []
  fixture.state = []
  fixture.pages = {
    '/reports': ({ project }) => ({
      data: { items: project === 'EMPTY' ? [] : [report(project, project === 'T' ? 1 : 2, project === 'T' ? 8 : 9)] },
    }),
    '/reports/pending': (filters, options) => {
      pendingScopes.push([filters.owner, filters.project, options.firstPage.next_cursor])
      return { data: options.firstPage, loadMore() {} }
    },
  }
  const markup = renderToStaticMarkup(React.createElement(Reports, { project: 'DEMO', projects }))
  assert.deepEqual(pendingScopes, [['Avery', 'T', 8], ['Avery', 'DEMO', 9]])
  assert.equal((markup.match(/<h1 id="reports">Reports<\/h1>/g) || []).length, 1)
  assert.match(markup, /class="report-project-label">Tasks Board<\/span>/)
  assert.match(markup, /class="report-project-label">Demo project<\/span>/)
  assert.match(markup, /href="#dri=Avery&amp;project=T">Avery/)
  assert.match(markup, /href="#dri=Avery&amp;project=DEMO">Avery/)
  assert.match(markup, /href="#project=T&amp;section=queue-heading&amp;owner=Avery">All tasks/)
  assert.match(markup, /href="#project=DEMO&amp;section=queue-heading&amp;owner=Avery">All tasks/)
  assert.match(markup, /href="#task-DEMO-1"/)
  assert.match(markup, /More reports for Avery in Tasks Board/)
  assert.match(markup, /More reports for Avery in Demo project/)
  assert.match(markup, /id="reports-EMPTY">Empty project.*No open work to report\./)
})

test('Reports shows the selector after its first success and retains a selected filter through an error', () => {
  function render(project, selection, result, expectedView) {
    fixture.state = [false, selection]
    fixture.pages = { '/reports': filters => {
      assert.deepEqual(filters, { project, view: expectedView })
      return result
    } }
    return renderToStaticMarkup(React.createElement(Reports, { project }))
  }

  const failure = { error: new Error('Try later.'), retry() {} }
  const courseFilter = { project: 'COURSE', view: 'all' }
  const initialError = render('T', null, failure, 'active')
  assert.match(initialError, /Unable to load reports.*Retry/)
  assert.doesNotMatch(initialError, /<select>/)
  assert.doesNotMatch(render('T', courseFilter, { pending: true }, 'active'), /<select>/)
  assert.doesNotMatch(render('T', courseFilter, failure, 'active'), /<select>/)
  assert.match(render('COURSE', courseFilter, failure, 'all'), /<option value="all" selected/)
})

test('Reports shows linked and unlinked asks in one request log without recipient-only notices', () => {
  const calls = []
  function feed(path, items) {
    return filters => {
      assert.deepEqual(filters, { project: 'COURSE' })
      calls.push(path)
      return { data: { items, next_cursor: 4 }, loadMore() {} }
    }
  }
  fixture.pages = {
    '/reports/request-log': feed('request log', [{
      id: 'review:1', source_id: 5, source_key: 'COURSE-2', source_title: 'Review samples',
      recipient: 'Morgan', next_owner: 'Morgan', review: true, phase: 'start_unconfirmed',
    }, {
      id: 'peer:2', source_id: 6, source_key: 'COURSE-3', source_title: 'Review the changes',
      request_title: 'Verify the export on a phone',
      request_key: 'COURSE-4', requester: 'Morgan', recipient: 'Avery', next_owner: 'Morgan', phase: 'acknowledgment',
      next_owner_profile: { name: 'Morgan', avatar_url: '/avatar/morgan' },
      response: { id: 11 },
    }, {
      id: 'peer:3', source_id: 8, source_key: 'COURSE-5', source_title: 'Check the sample',
      request_key: 'COURSE-6', requester: 'Morgan', recipient: 'Avery', phase: 'done',
      resolution: { id: 12 },
    }, {
      id: 'peer:4', source_id: 9, request_key: 'COURSE-7', recipient: 'Avery', phase: 'asked',
    }]),
    '/notifications/decisions': feed('decisions', [{
      id: 4, key: 'COURSE-1', title: 'Choose sample', note: 'Recipient-only decision.',
    }]),
    '/notifications/requests': feed('requests', [{
      id: 2, task_id: 5, task_key: 'COURSE-2', task_title: 'Review samples', reason: 'Recipient-only attention.',
    }]),
  }

  const render = (viewer, mode = 'compact') => {
    fixture.state = [mode]
    return renderToStaticMarkup(React.createElement(QueuedAsks, { project: 'COURSE', viewer }))
  }
  const reader = render(null)
  assert.deepEqual(calls, ['request log'])
  assert.equal((reader.match(/aria-label="Requests"/g) || []).length, 1)
  assert.match(reader, /href="#task-COURSE-2"/)
  assert.match(reader, /Next for Morgan/)
  assert.match(reader, /Start unconfirmed/)
  assert.match(reader, /href="#task-COURSE-4"[^>]*>Verify the export on a phone<\/a>/)
  assert.match(reader, /Ready to acknowledge/)
  assert.match(reader, /src="\/avatar\/morgan"/)
  assert.match(reader, /request-terminal[\s\S]*href="#task-COURSE-6"/)
  assert.doesNotMatch(reader, /request-details|<summary|href="#event-(11|12)-log"/)
  assert.match(reader, /aria-label="More requests"/)
  assert.doesNotMatch(reader, /Recipient-only/)
  const signedIn = render({ id: 'Avery' })
  assert.deepEqual(calls, ['request log', 'request log'])
  assert.doesNotMatch(signedIn, /Recipient-only/)
  assert.match(signedIn, /href="#task-COURSE-4"/)
  assert.doesNotMatch(reader, /class="request-action"/)
  assert.doesNotMatch(signedIn, /class="request-action"/)
  const requester = render({ id: 'Morgan' })
  assert.equal((requester.match(/class="request-action"/g) || []).length, 1)
  assert.match(requester, /class="request-action" href="#event-11-log">Review response<\/a>/)
  const verbose = render({ id: 'Morgan' }, 'verbose')
  assert.match(verbose, /href="#task-COURSE-4"[^>]*><span class="ticket-id">COURSE-4<\/span> Verify the export on a phone<\/a>/)
  assert.equal((verbose.match(/href="#task-COURSE-4"/g) || []).length, 1)
  assert.match(verbose, /href="#task-COURSE-3"><span class="ticket-id">COURSE-3<\/span> Review the changes<\/a>/)
  assert.match(verbose, /href="#event-11-log">Response<\/a>/)
  assert.match(verbose, /href="#task-COURSE-7"[^>]*><span class="ticket-id">COURSE-7<\/span><\/a>/)
})

test('a legacy hold, decision context, and evidence remain accessible', () => {
  const note = 'Checks passed. Do not deploy ' + 'a'.repeat(64) + ' until approval.'
  const hold = { ...selected, note, headline: undefined,
    links: [{ label: 'Hold evidence', url: 'https://example.test/hold' }] }
  const holdMarkup = renderLog([hold], null)
  assert.match(holdMarkup, /Hold recorded/)
  assert.ok(holdMarkup.includes(note))
  assert.equal(holdMarkup.split('Hold evidence').length - 1, 1)

  const decision = { ...selected, action: 'decide', context: { choice: 'CSV', decision_reason: 'Keep the existing import.' } }
  const decisionMarkup = renderLog([decision], null)
  assert.match(decisionMarkup, /<summary>Details<\/summary>/)
  assert.match(decisionMarkup, /Keep the existing import\./)
})

test('ticket detail uses the current record and keeps sources and evidence', () => {
  fixture.resources = { '/tasks/1': { data: {
    task: { id: 1, key: 'T-1', title: 'Check the CSV', state: 'in_progress', summary_label: 'Current step',
      note: 'Verify the updated header.', dri: 'Morgan', links: [{ label: 'Sample', url: 'https://example.test/sample' }] },
    brief: 'Compare the sample rows.', checklist: [], workers: [],
    sources: [{ label: 'Runbook', url: 'https://example.test/runbook' }],
    tasknote: { label: 'Edited TaskNote', url: '/old-note' },
  } } }
  fixture.pages = { '/tasks/1/activity': { data: { items: [selected] } } }
  fixture.state = []
  const markup = renderToStaticMarkup(React.createElement(TaskDetail, { id: 1, session: {} }))
  assert.match(markup, /Verify the updated header\./)
  assert.match(markup, /Compare the sample rows\./)
  assert.match(markup, /The header was fixed\./)
  assert.match(markup, /href="https:\/\/example\.test\/sample">Sample<\/a>/)
  assert.match(markup, /href="https:\/\/example\.test\/runbook">Runbook<\/a>/)
  assert.doesNotMatch(markup, /Edited TaskNote|\/old-note|Current status below/)
  assert.doesNotMatch(markup, /class="detail-checklist"/)
})

test('ticket editors share the signed guidance read for the ticket project', () => {
  const path = '/projects/COURSE/field-guidance'
  fixture.resources = {
    '/tasks/7': { data: {
      task: { id: 7, key: 'COURSE-7', title: 'Review the sample', state: 'in_progress', summary_label: 'Current step' },
      brief: 'Original instructions.', checklist: [], sources: [], workers: [], can_edit_details: true,
    } },
    [path]: { data: { fields: {
      title: { guidance: 'Start with the action.', where: 'List rows.', example: 'Review the sample.' },
      brief: { guidance: 'Describe the outcome.', where: 'Ticket details.', example: 'Check two viewports.' },
      activity: { guidance: 'Report what changed.', where: 'Activity feed.', example: 'All checks passed.' },
    } } },
  }
  fixture.pages = {}
  const render = kind => {
    fixture.state = []
    return renderToStaticMarkup(React.createElement(TaskDetail, { id: 7, session: { data: { actor: { kind, id: 'Avery' } } } }))
  }
  try {
    fixture.resourceReads = []
    const signed = render('agent')
    assert.deepEqual(fixture.resourceReads.filter(path => path?.endsWith('/field-guidance')), [path])
    assert.match(signed, /aria-describedby="ticket-title-guidance-7".*id="ticket-title-guidance-7".*Start with the action\..*List rows\..*Review the sample\./)
    assert.match(signed, /aria-describedby="ticket-brief-guidance-7".*id="ticket-brief-guidance-7".*Describe the outcome\..*Ticket details\..*Check two viewports\./)
    const activityHelpId = signed.match(/<label>Update<textarea[^>]+aria-describedby="([^"]+)"/)[1]
    assert.ok(signed.includes(`id="${activityHelpId}"><p>Report what changed.</p><p>Activity feed.</p><p><strong>Example</strong> All checks passed.</p>`))

    fixture.resourceReads = []
    assert.doesNotMatch(render('portal'), /field-guidance-help/)
    assert.equal(fixture.resourceReads.filter(path => path?.endsWith('/field-guidance')).length, 0)

    fixture.resources[path] = { error: { status: 404, message: 'Not found' } }
    const olderServer = render('agent')
    assert.match(olderServer, /Save details.*Post update/)
    assert.doesNotMatch(olderServer, /field-guidance-help|Not found/)
  } finally {
    delete fixture.resourceReads
  }
})

test('ticket detail opens its checklist with a progress count and readable step states', () => {
  const started = '2026-11-21T01:00:00Z'
  const completed = '2026-11-21T02:00:00Z'
  fixture.resources = { '/tasks/1': { data: {
    task: { id: 1, key: 'T-1', title: 'Check the CSV', state: 'in_progress', dri: 'Morgan',
      checklist_completed: 1, checklist_total: 3 },
    checklist: [
      { position: 1, label: 'Read the input', checked: 1, started_at: started, completed_at: completed,
        started_by: 'Avery', started_by_name: 'Avery Reader', completed_by: 'Blake', completed_by_name: 'Blake Writer' },
      { position: 2, label: 'Check the sums', started_at: started },
      { position: 3, label: 'Write the result' },
    ],
    sources: [], workers: [],
  } } }
  fixture.pages = {}
  fixture.state = []
  const markup = renderToStaticMarkup(React.createElement(TaskDetail, { id: 1, session: {} }))
  assert.match(markup, /<details class="detail-checklist" open=""><summary><span>Checklist<\/span><span>1 of 3 complete<\/span><\/summary>/)
  assert.match(markup, /aria-label="Complete"><svg[^>]*>.*?<\/svg><\/span><span class="checklist-step-number">1<\/span>.*?Read the input/)
  assert.match(markup, /aria-label="In progress"><svg[^>]*>.*?<\/svg><\/span><span class="checklist-step-number">2<\/span>.*?Check the sums/)
  assert.match(markup, /aria-label="Pending"><svg[^>]*>.*?<\/svg><\/span><span class="checklist-step-number">3<\/span>.*?Write the result/)
  assert.match(markup, /by Avery Reader/)
  assert.match(markup, /by Blake Writer/)
})

test('ticket worker assignment keeps effort and original brief in the disclosure', () => {
  fixture.resources = { '/tasks/1': { data: {
    task: { id: 1, key: 'T-1', title: 'Check the CSV', state: 'in_progress', dri: 'Morgan' },
    checklist: [], sources: [], workers: [
      { id: 1, worker: 'River', status: 'planned', role: 'review', effort: 'high', brief: 'Verify **the rows**.' },
      { id: 2, worker: 'Sky', status: 'collected', role: 'build', brief: 'Legacy assignment.' },
    ],
  } } }
  fixture.pages = { '/tasks/1/activity': { data: { items: [] } } }
  fixture.state = []
  const markup = renderToStaticMarkup(React.createElement(TaskDetail, { id: 1, session: {} }))
  assert.match(markup, /<details><summary>Assignment<\/summary><div[^>]*><p><strong>Assigned effort<\/strong> · high/)
  assert.match(markup, /Verify <strong>the rows<\/strong>/)
  assert.match(markup, /Legacy assignment\./)
  assert.equal(markup.split('Assigned effort').length, 2)
})

test('compact ticket history keeps only additional legacy prose in Details', () => {
  const render = fields => renderToStaticMarkup(React.createElement(EventRow, {
    ticketActivity: true, event: { ...selected, headline: undefined, links: [], ...fields },
  }))
  const wait = render({ action: 'wait', waiting_on: 'Fixture reviewer', note: 'Send the revised CSV.' })
  assert.match(wait, /Wait note Fixture reviewer · Send the revised CSV\./)
  assert.equal(wait.split('Send the revised CSV.').length, 2)
  assert.doesNotMatch(wait, /<summary>Details/)
  const legacy = render({ task_title: 'Review export', action: 'done',
    note: 'Review export. I completed the work. The header now matches. Three rows were checked.' })
  assert.equal(legacy.split('The header now matches.').length, 2)
  assert.match(legacy, /<summary>Details<\/summary>/)
  assert.match(legacy, /Three rows were checked\./)
  const choice = render({ action: 'decide', note: 'CSV.', context: { choice: 'CSV' } })
  assert.doesNotMatch(choice, /CSV · CSV|<summary>Details/)
  const hold = render({ action: 'log', note: 'Two cases passed. Hold the upload. The third is missing.' })
  assert.equal(hold.split('Hold the upload.').length, 2)
  assert.match(hold, /Two cases passed\. The third is missing\./)
})

test('ticket activity shows photos or initials and a relative link with an exact timestamp', t => {
  t.mock.method(Date, 'now', () => Date.parse('2026-11-21T08:00:01Z'))
  fixture.resources = { '/tasks/1': { data: {
    task: { id: 1, key: 'T-1', title: 'Check the CSV', state: 'done', summary_label: 'Result' },
    checklist: [], workers: [], sources: [],
  } } }
  const at = '2026-11-21T07:59:00Z'
  const photo = '/tasks/api/v1/avatars/fixture/digest'
  fixture.pages = { '/tasks/1/activity': { data: { items: [
    { ...selected, actor: 'Portrait Agent', actor_profile: { avatar_url: photo }, at },
    { ...selected, id: 19, actor: 'Missing Photo', at },
  ] } } }
  const zone = createTimeZoneFormatters('America/Los_Angeles')
  for (const session of [{}, { data: { actor: { kind: 'agent', id: 'Signed-in agent' } } }]) {
    fixture.state = []
    const markup = renderToStaticMarkup(React.createElement(TimeZoneContext.Provider, { value: zone },
      React.createElement(TaskDetail, { id: 1, session })))
    assert.ok(markup.includes(`<img class="assignee-avatar" src="${photo}" width="20" height="20" alt=""/>Portrait Agent`))
    assert.match(markup, /aria-hidden="true">MP<\/span>Missing Photo/)
    assert.match(markup, /href="#event-18-log" aria-label="Link to .+ by Portrait Agent at /)
    const exact = zone.formatTimestamp(at)
    assert.ok(markup.includes(`<time dateTime="${at}" title="${exact}">1m ago`))
    assert.ok(markup.includes(`by Portrait Agent at ${exact}`))
  }
})

test('ticket history shows the first evidence link and keeps additional detail together', () => {
  fixture.resources = { '/tasks/1': { data: {
    task: { id: 1, key: 'T-1', title: 'Check the CSV', state: 'done', summary_label: 'Result' },
    checklist: [], workers: [], sources: [],
  } } }
  const evidence = { label: 'Receipt', url: 'https://example.test/receipt' }
  const short = { ...selected, details: selected.headline.replace(/\.$/, ''), links: [evidence] }
  const long = { ...short, id: 19, details: selected.headline + '\n\nThe fixture included 20 sample rows.',
    links: [evidence, { label: 'Sample', url: 'https://example.test/sample' }] }
  const decision = {
    ...selected, id: 20, action: 'decide', label: 'Decision recorded', headline: 'CSV', note: 'CSV',
    context: { choice: 'CSV', request_event_id: 8 },
  }
  const historical = { ...selected, id: 21, action: 'done', label: 'Completed', headline: undefined, note: '' }
  fixture.pages = { '/tasks/1/activity': { data: { items: [short, long, decision, historical] } } }
  fixture.state = []
  const ticket = renderToStaticMarkup(React.createElement(TaskDetail, { id: 1, session: {} }))
  const [, evidenceOnly, additional, answer, fallback] = ticket.split('<li class="event-row ticket-event-row">')
  assert.doesNotMatch(evidenceOnly, /<summary>Details<\/summary>/)
  assert.match(evidenceOnly, /href="https:\/\/example\.test\/receipt">Receipt<\/a>/)
  assert.match(additional, /href="https:\/\/example\.test\/receipt">Receipt<\/a>.*<summary>Details<\/summary>.*href="https:\/\/example\.test\/sample">Sample<\/a>/s)
  assert.equal(additional.split(selected.headline).length, 2)
  assert.match(additional, /The fixture included 20 sample rows\./)
  assert.match(answer, /class="activity-result">CSV<\/p>/)
  assert.match(answer, /href="#event-8-log">Decision request<\/a>/)
  assert.doesNotMatch(answer, /Selected CSV|<strong>CSV<\/strong>/)
  assert.match(fallback, /class="activity-result">Completed<\/p>/)
  assert.doesNotMatch(fallback, /<summary>Details<\/summary>/)

  const feed = renderToStaticMarkup(React.createElement(EventRow, { event: short }))
  assert.doesNotMatch(feed, /ticket-event-row/)
  assert.match(feed, /<\/details><ul class="evidence-links"[^>]*><li><a href="https:\/\/example\.test\/receipt">Receipt<\/a>/)
})

test('a task with no owner shows Unassigned in the queue and detail at any open state', () => {
  const task = { id: 7, key: 'COURSE-7', title: 'Review the sample', note: 'Check the rows.', priority: 'high',
    summary_label: 'Next step', dri: '', dri_profile: null, links: [] }
  function detail(record) {
    fixture.resources = { '/tasks/COURSE-7': { data: { checklist: [], sources: [], workers: [], ...record } } }
    fixture.state = []
    return renderToStaticMarkup(React.createElement(TaskDetail, { id: task.key, session: {} }))
  }
  fixture.pages = { '/assignees': () => assert.fail('the closed disclosure must not load agents') }
  for (const state of ['ready', 'parked', 'waiting', 'human_review']) {
    const unassigned = { ...task, state }
    fixture.state = []
    const row = renderToStaticMarkup(React.createElement(TaskRow, { task: unassigned }))
    assert.match(row, /class="assignee">Unassigned<\/span>/)
    assert.match(detail({ task: unassigned, ownership_actions: [] }), /class="assignee">Unassigned<\/span>/)
    assert.doesNotMatch(row, /#dri=/)
  }
  const readOnly = detail({ task: { ...task, state: 'ready' }, ownership_actions: [] })
  assert.doesNotMatch(readOnly, /Change ownership|#dri=/)
  const allowed = detail({ task: { ...task, state: 'ready' }, ownership_actions: ['reassign'] })
  assert.match(allowed, /<summary>Change ownership<\/summary>/)
  assert.match(allowed, /<option value="reassign" selected="">Reassign<\/option>/)
  assert.doesNotMatch(allowed, /<option value="(?:unassign|reopen)"/)
})

test('the Unassigned filter applies to list and Kanban without sending an owner', () => {
  const calls = []
  fixture.pages = { '/tasks': filters => {
    calls.push(queryPath('/tasks', filters))
    return { data: { items: [] } }
  } }
  const props = {
    filters: { state: 'ready', dri: 'Avery', unassigned: true, waiting_on: 'Sample blocker' },
    overview: { owners: ['unassigned'] },
    waitingLabel: 'Sample blocker',
    project: 'COURSE',
  }
  for (const layout of ['list', 'kanban']) {
    fixture.state = [{}, false]
    const markup = renderToStaticMarkup(React.createElement(WorkQueue, { ...props, layout }))
    assert.match(markup, /value="unassigned" selected="">Unassigned<\/option>/)
    assert.match(markup, /value="owner:unassigned">unassigned<\/option>/)
    assert.equal(calls.length, 1)
    const params = new URLSearchParams(calls.pop().split('?')[1])
    assert.equal(params.get('unassigned'), 'true')
    assert.equal(params.has('dri'), false)
    assert.equal(params.get('project'), 'COURSE')
    assert.equal(params.get('state'), 'ready')
    assert.equal(params.get('order'), 'priority')
    assert.equal(params.get('waiting_on'), 'Sample blocker')
  }
})

test('Closed is a distinct filter and Kanban column when the board supports it', () => {
  const calls = []
  fixture.resources = {}
  fixture.pages = { '/tasks': filters => {
    calls.push(filters.state)
    return { data: { items: [] } }
  } }
  for (const layout of ['list', 'kanban']) {
    fixture.state = [{}, false]
    const filtered = renderToStaticMarkup(React.createElement(WorkQueue, {
      filters: { state: 'closed' }, overview: { counts: { done: 2, closed: 0 } }, project: 'T', layout,
    }))
    assert.match(filtered, /value="closed" selected="">Closed \(0\)<\/option>/)
    assert.deepEqual(calls.splice(0), ['closed'])
  }
  fixture.state = [{}, false]
  const all = renderToStaticMarkup(React.createElement(WorkQueue, {
    filters: {}, overview: { counts: { closed: 1 } }, project: 'T', layout: 'kanban',
  }))
  assert.match(all, /<h3 id="kanban-closed">Closed<\/h3>/)
  assert.equal(calls.splice(0).filter(state => state === 'closed').length, 1)

  fixture.state = [{}, true]
  const phone = renderToStaticMarkup(React.createElement(WorkQueue, {
    filters: {}, overview: { counts: { closed: 1 } }, project: 'T', layout: 'kanban', phoneColumn: 'closed',
  }))
  assert.match(phone, /value="closed" selected="">Closed<\/option>/)
  assert.match(phone, /<h3 id="kanban-closed">Closed<span class="kanban-count"> \(1\)<\/span><\/h3>/)
  assert.deepEqual(calls.splice(0), ['closed'])

  fixture.state = [{}, true]
  const byOwner = renderToStaticMarkup(React.createElement(WorkQueue, {
    filters: { dri: 'Avery' }, overview: { counts: { closed: 1 } }, project: 'T', layout: 'kanban', phoneColumn: 'closed',
  }))
  assert.match(byOwner, /<h3 id="kanban-closed">Closed<\/h3>/)
  assert.deepEqual(calls.splice(0), ['closed'])

  fixture.state = [{}, false]
  const olderBoard = renderToStaticMarkup(React.createElement(WorkQueue, {
    filters: {}, overview: { counts: { done: 2 } }, project: 'T', layout: 'kanban',
  }))
  assert.doesNotMatch(olderBoard, /kanban-closed|value="closed"/)
  assert.ok(!calls.includes('closed'))
})

test('closed tickets show their reason and related ticket and retain the manager reopen action', () => {
  const context = { closure: { related: 'COURSE-2' } }
  const task = {
    id: 7, key: 'COURSE-7', title: 'Duplicate sample', state: 'done', display_state: 'closed',
    note: 'The other ticket tracks the same work.', context, links: [],
  }
  fixture.state = []
  const row = renderToStaticMarkup(React.createElement(TaskRow, { task }))
  assert.match(row, /class="status closed">Closed<\/span>/)
  assert.match(row, /The other ticket tracks the same work\./)
  assert.match(row, /href="#task-COURSE-2"><span class="ticket-id">COURSE-2<\/span>/)
  for (const [state, displayState, canClose, actions] of [
    ['done', 'closed', true, ['reopen']], ['done', undefined, true, ['reopen']],
    ['ready', undefined, false, []], ['ready', undefined, true, []],
  ]) {
    fixture.resources = { '/tasks/COURSE-7': { data: {
      task: { ...task, state, display_state: displayState, context: displayState ? context : {} },
      can_close: canClose, ownership_actions: actions, checklist: [], sources: [], workers: [],
    } } }
    fixture.pages = {}
    fixture.state = []
    const detail = renderToStaticMarkup(React.createElement(TaskDetail, { id: task.key, session: {} }))
    if (displayState) {
      assert.match(detail, /aria-label="Closure reason"><h2>Closure reason<\/h2>/)
      assert.match(detail, /href="#task-COURSE-2"/)
      assert.match(detail, /<option value="reopen" selected="">Reopen<\/option>/)
    }
    if (state === 'ready' && canClose) assert.match(detail, /<summary>Close task<\/summary>/)
    else assert.doesNotMatch(detail, /<summary>Close task<\/summary>/)
  }
  const event = { ...selected, action: 'update', label: 'Closed', context, note: task.note, headline: task.note }
  for (const ticketActivity of [false, true]) {
    const activity = renderToStaticMarkup(React.createElement(EventRow, { event, ticketActivity }))
    assert.match(activity, /aria-label="Closed" title="Closed"><svg[^>]*class="lucide lucide-x"/)
    assert.match(activity, /The other ticket tracks the same work\./)
    assert.match(activity, /href="#task-COURSE-2"/)
  }
})

test('closing a task submits its reason and optional related ticket and retains the draft on error', async () => {
  const task = { id: 7, key: 'COURSE-7', state: 'ready' }
  const sent = []
  const updates = []
  let fail = false
  let changed = 0
  const props = { task, onChanged: () => { changed += 1 } }
  const event = { preventDefault() {} }
  fixture.request = async (path, options) => {
    sent.push({ path, ...options })
    if (fail) throw new Error('The related ticket could not be found.')
  }
  fixture.onSetState = value => updates.push(value)
  function form(reason, related, saving = false) {
    fixture.state = [reason, related, saving, null]
    return React.Children.toArray(TaskClosure(props).props.children).find(child => child.type === 'form')
  }
  try {
    await form('   ', 'COURSE-1').props.onSubmit(event)
    await form('The work was dropped.', '', true).props.onSubmit(event)
    assert.equal(sent.length, 0)
    await form('  The work was dropped.  ', ' ').props.onSubmit(event)
    assert.equal(changed, 1)
    fail = true
    updates.length = 0
    await form('Duplicate work.', ' COURSE-1 ').props.onSubmit(event)
    assert.deepEqual(sent, [
      { path: '/tasks/COURSE-7/closure', method: 'POST', body: { reason: 'The work was dropped.' } },
      { path: '/tasks/COURSE-7/closure', method: 'POST', body: { reason: 'Duplicate work.', related: 'COURSE-1' } },
    ])
    assert.equal(changed, 1)
    assert.deepEqual(updates, [true, null, 'The related ticket could not be found.', false])
    fixture.state = ['Duplicate work.', ' COURSE-1 ', false, updates[2]]
    const failed = renderToStaticMarkup(React.createElement(TaskClosure, props))
    assert.match(failed, /data-reload-blocked="true"/)
    assert.match(failed, /Duplicate work\.<\/textarea>/)
    assert.match(failed, /value=" COURSE-1 "/)
    assert.match(failed, /role="alert">The related ticket could not be found\./)
  } finally {
    delete fixture.request
    delete fixture.onSetState
  }
})

test('editing task details sends only changed fields and keeps the draft if the save fails', async () => {
  const sent = []
  const updates = []
  let changed = 0
  const props = { task: { id: 7, key: 'COURSE-7', title: 'Keep the title' }, brief: 'Old brief',
    onChanged: () => { changed += 1 } }
  const event = { preventDefault() {} }
  fixture.request = async (path, options) => {
    sent.push({ path, ...options })
    if (sent.length === 2) throw new Error('The edit could not be saved.')
  }
  fixture.onSetState = value => updates.push(value)
  function form(draft, reason) {
    fixture.state = [draft, reason, false, null]
    return React.Children.toArray(TaskDetailsEdit(props).props.children).find(child => child.type === 'form')
  }
  try {
    await form({ brief: '' }, ' ').props.onSubmit(event)
    assert.equal(sent.length, 0)
    await form({ brief: '' }, 'Clear the old instructions.').props.onSubmit(event)
    assert.equal(changed, 1)
    updates.length = 0
    await form({ title: 'Corrected title' }, 'Correct the scope.').props.onSubmit(event)
    assert.deepEqual(sent, [
      { path: '/tasks/COURSE-7/details', method: 'POST', body: { brief: '', reason: 'Clear the old instructions.' } },
      { path: '/tasks/COURSE-7/details', method: 'POST', body: { title: 'Corrected title', reason: 'Correct the scope.' } },
    ])
    assert.deepEqual(updates, [true, null, 'The edit could not be saved.', false])
    fixture.state = [{ title: 'Corrected title' }, 'Correct the scope.', false, updates[2]]
    const failed = renderToStaticMarkup(React.createElement(TaskDetailsEdit, props))
    assert.match(failed, /value="Corrected title"/)
    assert.match(failed, /role="alert">The edit could not be saved\./)
  } finally {
    delete fixture.request
    delete fixture.onSetState
  }
})

test('title edits submit longer titles intact and preserve unchanged older titles', async () => {
  const original = 'x'.repeat(85)
  const longer = 'Verify the mounted review page on phone and desktop after the rollout'
  const props = { task: { id: 8, key: 'T-8', title: original }, brief: 'Old brief', onChanged() {} }
  const sent = []
  fixture.request = async (_, options) => sent.push(options.body)
  const event = { preventDefault() {} }
  const edit = async draft => {
    fixture.state = [draft, 'Clarify the scope.', false, null]
    const markup = renderToStaticMarkup(React.createElement(TaskDetailsEdit, props))
    fixture.state = [draft, 'Clarify the scope.', false, null]
    const form = React.Children.toArray(TaskDetailsEdit(props).props.children).find(child => child.type === 'form')
    await form.props.onSubmit(event)
    return markup
  }
  try {
    assert.match(await edit({ brief: 'New brief' }), new RegExp(original))
    const edited = await edit({ title: longer })
    assert.match(edited, /<button type="submit">Save details<\/button>/)
    assert.doesNotMatch(edited, /aria-invalid="true"|Keep the title to 60 characters/)
    assert.deepEqual(sent, [
      { brief: 'New brief', reason: 'Clarify the scope.' },
      { title: longer, reason: 'Clarify the scope.' },
    ])
  } finally {
    delete fixture.request
  }
})

test('the Tag filter uses the current project in both task layouts', () => {
  const calls = []
  fixture.resources = {
    '/tags?project=COURSE': { data: { tags: ['reflection', 'validation'] } },
    '/tags?project=T': { data: { tags: ['release'] } },
  }
  fixture.pages = { '/tasks': filters => {
    calls.push(queryPath('/tasks', filters))
    return { data: { items: [] } }
  } }
  const props = { filters: { state: 'ready', tag: { project: 'COURSE', name: 'reflection' } } }
  for (const layout of ['list', 'kanban']) {
    for (const project of ['COURSE', 'T']) {
      fixture.state = [{}, false]
      const markup = renderToStaticMarkup(React.createElement(WorkQueue, { ...props, project, layout }))
      assert.match(markup, /<label>Tag<select>/)
      assert.equal(calls.length, 1)
      const params = new URLSearchParams(calls.pop().split('?')[1])
      assert.equal(params.get('project'), project)
      assert.equal(params.get('tag'), project === 'COURSE' ? 'reflection' : null)
      if (project === 'COURSE') {
        assert.match(markup, /value="reflection" selected=""/)
        assert.match(markup, /value="validation"/)
      } else {
        assert.match(markup, /value="" selected="">All tags/)
        assert.doesNotMatch(markup, /reflection|validation/)
      }
    }
  }
})

test('the ticket shows its tags and enables editing only when allowed', () => {
  const task = { id: 7, key: 'COURSE-7', title: 'Review the sample', state: 'ready',
    summary_label: 'Next step', tags: ['reflection'] }
  const detail = (task, editable) => {
    fixture.resources = { '/tasks/COURSE-7': { data: {
      task, tag_editable: editable, checklist: [], sources: [], workers: [],
    } } }
    fixture.pages = {}
    fixture.state = []
    return renderToStaticMarkup(React.createElement(TaskDetail, { id: task.key, session: {} }))
  }
  assert.match(detail(task, false), /aria-label="Task tags".*<span>reflection<\/span>/)
  assert.doesNotMatch(detail(task, false), /Remove tag reflection|Add tag/)
  const editable = detail(task, true)
  assert.match(editable, /aria-label="Remove tag reflection"/)
  assert.match(editable, /<label>Tag<input[^>]*maxLength="32"/)
  assert.match(editable, />Add tag<\/button>/)
  assert.doesNotMatch(detail({ ...task, tags: undefined }, undefined), /Task tags|Add tag/)
})

test('tag edits use the exact ticket route and retain an invalid or unsaved draft', async () => {
  const task = { id: 7, key: 'COURSE-7', tags: ['reflection'] }
  const sent = []
  const updates = []
  let changed = 0
  let fail = false
  fixture.request = async (path, request) => {
    sent.push({ path, ...request })
    if (fail) throw new Error('Tag could not be saved.')
  }
  fixture.onSetState = value => updates.push(value)
  const props = { task, editable: true, onChanged: () => { changed += 1 } }
  const event = { preventDefault() {} }
  function controls(draft) {
    fixture.state = [draft, false, null]
    const [list, form] = TaskTags(props).props.children
    const remove = list.props.children[0].props.children[1]
    return { remove, form }
  }

  try {
    await controls('two words').form.props.onSubmit(event)
    assert.equal(sent.length, 0)
    assert.equal(updates[0].error, true)
    await controls(' Release-Review ').form.props.onSubmit(event)
    await controls('').remove.props.onClick()
    assert.equal(changed, 2)
    assert.deepEqual(sent, [
      { path: '/tasks/COURSE-7/tags', method: 'POST', body: { tag: 'release-review', action: 'add' } },
      { path: '/tasks/COURSE-7/tags', method: 'POST', body: { tag: 'reflection', action: 'remove' } },
    ])

    fail = true
    updates.length = 0
    await controls('release-review').form.props.onSubmit(event)
    assert.equal(changed, 2)
    assert.deepEqual(updates, [true, null, { error: true, text: 'Tag could not be saved.' }, false])
    fixture.state = ['release-review', false, updates[2]]
    const failure = renderToStaticMarkup(React.createElement(TaskTags, props))
    assert.match(failure, /value="release-review"/)
    assert.match(failure, /role="alert">Tag could not be saved\./)
  } finally {
    delete fixture.request
    delete fixture.onSetState
  }
})

test('ownership forms post allowed changes and keep the reason and owner after failure', async () => {
  const owner = 'raft:agent:fixture-target'
  const task = { id: 7, key: 'COURSE-7', state: 'in_progress', dri: 'raft:agent:fixture-owner' }
  const reason = '  Hand off the sample review.  '
  const sent = []
  const updates = []
  let fail = false
  let changed = 0
  let directoryReads = 0
  fixture.pages = { '/assignees': filters => {
    assert.deepEqual(filters, { project: 'COURSE' })
    directoryReads += 1
    return { data: { items: [
      { kind: 'agent', name: 'Sample Reviewer', id: owner },
      { kind: 'agent', name: 'Original Owner', id: task.dri },
      { kind: 'agent', name: 'raft:agent:missing-name', id: 'raft:agent:missing-name' },
      { kind: 'human', name: 'Sample Human', id: 'raft:human:fixture-human' },
    ] } }
  } }
  fixture.request = async (path, request) => {
    sent.push({ path, ...request })
    if (fail) throw new Error('The task changed. Try again.')
  }
  fixture.onSetState = value => updates.push(value)
  const props = action => ({ task, actions: [action], project: 'COURSE', onChanged: () => { changed += 1 } })
  function form(action, draftOwner, draftReason) {
    fixture.state = [true, '', draftOwner, draftReason, false, null]
    const screen = TaskOwnership(props(action))
    return React.Children.toArray(screen.props.children).find(child => child.type === 'form')
  }
  const event = { preventDefault() {} }
  try {
    await form('unassign', owner, '   ').props.onSubmit(event)
    await form('reassign', '', reason).props.onSubmit(event)
    assert.equal(sent.length, 0)
    assert.equal(directoryReads, 1)

    await form('unassign', owner, reason).props.onSubmit(event)
    await form('reopen', owner, reason).props.onSubmit(event)
    assert.equal(changed, 2)

    fail = true
    updates.length = 0
    await form('reassign', owner, reason).props.onSubmit(event)
    assert.deepEqual(sent, [
      { path: '/tasks/COURSE-7/ownership', method: 'POST', body: { action: 'unassign', reason: reason.trim() } },
      { path: '/tasks/COURSE-7/ownership', method: 'POST', body: { action: 'reopen', reason: reason.trim(), owner } },
      { path: '/tasks/COURSE-7/ownership', method: 'POST', body: { action: 'reassign', reason: reason.trim(), owner } },
    ])
    assert.equal(changed, 2)
    assert.deepEqual(updates, [true, null, { error: true, text: 'The task changed. Try again.' }, false])
    fixture.state = [true, '', owner, reason, false, updates[2]]
    const failed = renderToStaticMarkup(React.createElement(TaskOwnership, props('reassign')))
    assert.match(failed, /data-reload-blocked="true"/)
    assert.match(failed, /role="alert">The task changed\. Try again\./)
    assert.ok(failed.includes(reason))
    assert.match(failed, /selected="">Sample Reviewer<\/option>/)
    assert.doesNotMatch(failed, /Sample Human|Original Owner|raft:agent:missing-name|>raft:/)
  } finally {
    delete fixture.request
    delete fixture.onSetState
  }
})

test('Needs me shares the decision form with the exact recipient and keeps the receipt when the row leaves', () => {
  const task = { id: 7, key: 'COURSE-7', event_id: 41, state: 'human_review', waiting_on: 'sam',
    title: 'Choose a format', note: 'Which format should we use?', context: { choices: ['CSV', 'Other'] }, links: [] }
  fixture.resources = { '/notifications/open-count?scope=all': { data: { count: 24 } } }
  function render(actor, items = [task], submitted = [], attempt = null, verified = true) {
    fixture.pages = { '/notifications/decisions': { data: { items } } }
    fixture.state = [submitted, null, attempt]
    return renderToStaticMarkup(React.createElement(Notifications, { session: { data: { actor } }, verified, fragment: 'notifications-needs-me' }))
  }
  const sam = { kind: 'human', id: 'sam' }
  const open = render(sam)
  assert.match(open, /href="#notifications-needs-me" aria-current="page"/)
  assert.match(open, /Needs me<span aria-live="polite"> \(24\)<\/span>/)
  assert.match(open, /Which format should we use\?/)
  assert.match(open, /name="decision-choice" value="CSV"/)
  assert.match(open, /name="decision-choice" value="Other"/)
  assert.match(open, /Other \(write an answer\)/)
  assert.match(open, /<textarea rows="3" maxLength="180" required=""><\/textarea>/)
  assert.match(open, /<button type="submit" disabled="">Record decision<\/button>/)
  assert.doesNotMatch(render({ kind: 'human', id: 'other' }), /Record decision/)
  assert.doesNotMatch(render({ kind: 'agent', id: 'sam' }), /Record decision/)
  assert.doesNotMatch(render(null, [task], [], null, false), /Which format should we use\?|Record decision|Mark handled/)
  const saved = { status: 'saved', recipient: 'sam', request: { id: task.event_id } }
  const retained = render(sam, [], [task], saved)
  assert.match(retained, /Decision recorded\./)
  assert.doesNotMatch(retained, /No open decision requests|Record decision/)
})

test('a lost decision response checks the same request and unlocks a retry only while open', async () => {
  const task = { id: 7, key: 'COURSE-7', event_id: 41, state: 'human_review',
    waiting_on: 'sam', context: { choices: ['CSV', 'JSON'] } }
  const session = { data: { actor: { kind: 'human', id: 'sam' } } }
  const changedTask = { ...task, event_id: 43, state: 'ready', waiting_on: null }
  const newRequest = { ...task, event_id: 44 }
  const event = { preventDefault() {} }
  const form = tree => React.Children.toArray(tree.props.children).find(child => child.type === 'form')

  for (const scenario of ['open', 'open-other', 'answered', 'closed', 'superseded-after-open']) {
    const other = scenario === 'open-other'
    const draft = { id: 41, choice: other ? '' : 'CSV', other, note: 'My answer', details: 'My draft details' }
    const status = ['open-other', 'superseded-after-open'].includes(scenario) ? 'open' : scenario
    const sent = []
    const updates = []
    const effects = []
    let completeRead
    let changes = 0
    let attempts = 0
    const post = { current: null }
    const lastRequest = { current: undefined }
    fixture.captureEffect = effect => effects.push(effect)
    fixture.onSetState = update => updates.push(update)
    fixture.request = async (path, options) => {
      sent.push({ path, ...options })
      if (!options.body) return new Promise(resolve => { completeRead = resolve })
      if (sent.length === 1) throw Object.assign(new Error('Bad Gateway'), { status: 502 })
      return { receipt: 'Decision recorded' }
    }

    function render(record, attempt = null, currentDraft = draft, currentSession = session) {
      effects.length = 0
      fixture.state = [currentDraft, attempt]
      fixture.refs = [post, lastRequest]
      const tree = DecisionForm({ task: record, session: currentSession, onChanged: () => { changes += 1 },
        onAnswerStarted: () => { attempts += 1 } })
      effects[0]()
      return tree
    }

    try {
      const original = form(render(task))
      await original.props.onSubmit(event)
      const checking = updates.at(-1)
      assert.equal(checking.status, 'checking')
      await original.props.onSubmit(event)
      assert.equal(sent.length, 1)
      assert.equal(attempts, 1)
      const pending = render(status === 'open' ? task : changedTask, checking)
      const waiting = renderToStaticMarkup(pending)
      assert.match(waiting, /Checking whether your answer was recorded/)
      assert.match(waiting, /<button type="submit" disabled="">Record decision<\/button>/)
      assert.match(waiting, />My answer<\/textarea>/)
      assert.match(waiting, /My draft details/)
      assert.doesNotMatch(waiting, /Retry recording/)

      const missingTask = renderToStaticMarkup(render(undefined, checking))
      assert.match(missingTask, /Checking whether your answer was recorded/)
      assert.match(missingTask, />My answer<\/textarea>/)
      const stop = effects.at(-1)()
      assert.equal(sent[1].path, '/tasks/COURSE-7/decision/41')
      assert.equal(sent[1].cache, 'no-store')
      assert.equal(sent[1].body, undefined)
      completeRead({ status, answer: status === 'answered' ? {
        event_id: 42, at: '2026-11-21T07:59:00Z', note: 'The recorded answer', choice: 'JSON',
      } : null })
      await new Promise(setImmediate)
      stop()
      assert.equal(sent.length, 2)
      const recovered = updates.findLast(update => update?.status === status)
      assert.ok(recovered)
      assert.equal(changes, status === 'open' ? 0 : 1)
      const view = render(status === 'open' ? task : changedTask, recovered, status === 'answered' ? null : draft)
      const result = renderToStaticMarkup(view)
      if (scenario === 'superseded-after-open') {
        assert.match(result, /Retry recording/)
        const superseded = render(newRequest, recovered)
        const verifying = renderToStaticMarkup(superseded)
        assert.match(verifying, /Checking whether your answer was recorded/)
        assert.match(verifying, />My answer<\/textarea>/)
        assert.doesNotMatch(verifying, /Retry recording/)
        await form(view).props.onSubmit(event)
        assert.equal(sent.length, 2)
        const stopSuperseded = effects.at(-1)()
        assert.equal(sent[2].path, '/tasks/COURSE-7/decision/41')
        completeRead({ status: 'closed', answer: null })
        await new Promise(setImmediate)
        stopSuperseded()
        const closed = updates.findLast(update => update?.status === 'closed')
        const newMarkup = renderToStaticMarkup(render(newRequest, closed))
        assert.match(newMarkup, /closed elsewhere/)
        assert.match(newMarkup, /My draft details/)
        assert.match(newMarkup, /<textarea rows="3" maxLength="180" required=""><\/textarea>/)
        assert.doesNotMatch(newMarkup, /Retry recording/)
        assert.equal(changes, 1)
      } else if (status === 'open') {
        assert.match(result, /<button type="submit">Retry recording<\/button>/)
        await form(view).props.onSubmit(event)
        assert.deepEqual(sent[2].body, sent[0].body)
        assert.deepEqual(sent[2].body, { request_id: 41, note: 'My answer', details: 'My draft details',
          ...(other ? { other: true } : { choice: 'CSV' }) })
        assert.equal(attempts, 2)
        assert.equal(changes, 1)
      } else {
        assert.equal(form(view), undefined)
        if (status === 'answered') {
          assert.match(result, /href="#event-42-log">View your answer<\/a>/)
          assert.match(result, /The recorded answer/)
          assert.doesNotMatch(result, /My answer/)
        } else {
          assert.match(result, /closed elsewhere/)
          assert.match(result, /My draft details/)
        }
      }
      const otherViewer = renderToStaticMarkup(render(changedTask, recovered, draft, { data: { actor: { kind: 'human', id: 'other' } } }))
      assert.doesNotMatch(otherViewer, /View your answer|My draft|Retry recording|closed elsewhere/)
    } finally {
      delete fixture.refs
      delete fixture.captureEffect
      delete fixture.onSetState
      delete fixture.request
      fixture.state = []
    }
  }
})

test('My activity retains provider timestamps and generic source labels', () => {
  const historical = { ...selected, action: 'source-event', label: 'Source update',
    context: { source_kind: 'local-file', provider_at: selected.at } }
  const other = { ...historical, id: 19, context: { source_kind: 'other-provider', provider_at: selected.at } }
  fixture.resources = {}
  fixture.pages = { '/my-activity': { data: { items: [historical, other], source_errors: [] } } }
  fixture.state = []
  const markup = renderToStaticMarkup(React.createElement(MyActivity, { revision: 0 }))
  assert.match(markup, /<span>Local file<\/span>/)
  assert.equal((markup.match(/Changed <time/g) || []).length, 2)
  assert.doesNotMatch(markup, /other-provider/)
})

test('Notifications distinguishes a failed session lookup from sign-in or access requirements', () => {
  const render = session => renderToStaticMarkup(React.createElement(Notifications, { session, verified: false }))
  const failed = render({ error: { message: 'Request failed.', status: 503 }, retry: () => {} })
  assert.match(failed, /Unable to load your session\. Request failed\./)
  assert.match(failed, /<button type="button">Retry<\/button>/)
  assert.doesNotMatch(failed, /verified Raft account|Sign in with Raft/)
  assert.match(render({ error: { status: 401 } }), /Sign in with Raft to view your notifications/)
  assert.match(render({ data: { sign_in_available: true } }), /Sign in with Raft to view your notifications/)
  assert.match(render({ data: { sign_in_available: false } }), /Notifications require a verified Raft account/)
})

test('readiness prompts link parked tickets, their dependencies, and the exact evidence events', () => {
  const tickets = [
    ['merged', 'Merge recorded'],
    ['deferred', 'Deferral recorded'],
    ['completed', 'Completion recorded'],
  ].map(([event_kind, label], index) => ({
    item: {
      id: index + 4, key: `COURSE-${index + 1}`, title: 'Check the release',
      criteria: 'Verify the reviewed build before resuming.',
      source_id: 2, source_key: 'T-2', source_title: 'Prepare the reviewed build',
      event_id: index + 10, event_kind,
    },
    label,
  }))
  fixture.state = []
  fixture.resources = { '/notifications/preferences': { data: { task_creations: false } } }
  fixture.pages = {
    '/notifications/inbox': { data: { items: tickets.map(({ item }) => ({ kind: 'readiness', id: item.id, item })) } },
  }
  const markup = renderToStaticMarkup(React.createElement(Notifications, { session: {}, verified: true }))
  assert.match(markup, /href="#task-COURSE-1".*Check the release/)
  assert.match(markup, /Check whether this task can move to Ready\./)
  assert.match(markup, /Verify the reviewed build before resuming\./)
  assert.match(markup, /href="#task-2".*Prepare the reviewed build/)
  for (const { item, label } of tickets) {
    assert.ok(markup.includes(`href="#event-${item.event_id}-log">${label}</a>`))
  }
  assert.doesNotMatch(markup, /Mark handled/)
})

function renderReminders(page) {
  fixture.pages = { '/notifications/task-reminders': page }
  fixture.resources = {}
  fixture.state = []
  return renderToStaticMarkup(React.createElement(Notifications, { session: {}, verified: true }))
}

test('reminder continuation keeps existing rows and disappears after the last page', () => {
  const task = { id: 1, title: 'Revisit the saved export', state: 'parked', dri: 'Avery' }
  const first = renderReminders({ data: { items: [task], next_cursor: 3 }, loadMore: () => {} })
  assert.match(first, /Revisit the saved export/)
  assert.match(first, /aria-label="More task reminders"[^>]*>More<\/button>/)

  const later = { ...task, id: 2, title: 'Check the source links' }
  const complete = renderReminders({ data: { items: [task, later], next_cursor: null } })
  assert.match(complete, /Revisit the saved export/)
  assert.match(complete, /Check the source links/)
  assert.doesNotMatch(complete, /aria-label="More task reminders"/)
})

test('loading and failed reminder requests remain visible while an empty first page stays quiet', () => {
  assert.match(renderReminders({ pending: true }), /Loading task reminders/)
  const failed = renderReminders({ error: new Error('Fixture request failed.'), retry: () => {} })
  assert.match(failed, /Unable to load task reminders\. Fixture request failed\./)
  assert.match(failed, /<button type="button">Retry<\/button>/)
  assert.doesNotMatch(renderReminders({ data: { items: [] } }), /id="reminders-title"/)
})
