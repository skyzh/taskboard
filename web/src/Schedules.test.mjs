import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

let vite
let ScheduleContents
let RecurringRunList
let signInHref

before(async () => {
  vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, ws: false, hmr: false, watch: null },
  })
  ;({ ScheduleContents } = await vite.ssrLoadModule('/src/Schedules.jsx'))
  ;({ RecurringRunList } = await vite.ssrLoadModule('/src/RecurringRuns.jsx'))
  ;({ signInHref } = await vite.ssrLoadModule('/src/shared.jsx'))
})

after(() => vite?.close())

const schedule = {
  id: 'R-1', title: 'Review samples', owner: 'owner-id', owner_name: 'River', editable: true,
  state: 'active', cadence_hours: 24, next_at: '2026-11-23T10:00:00Z',
  recent_occurrences: [
    { due_at: '2026-11-21T10:00:00Z', status: 'completed_on_time', task_id: 5, task_key: 'COURSE-1' },
    { due_at: '2026-11-20T10:00:00Z', status: 'missed', task_id: null },
  ],
}

function content(project, recurring, maintenance) {
  return renderToStaticMarkup(React.createElement(ScheduleContents, {
    project, data: { recurring, maintenance }, onChanged() {},
  }))
}

test('renders recurring work and keeps permitted edits and issued ticket links', () => {
  const html = content('COURSE', [schedule], { editable: false, schedule: null })
  assert.match(html, /Review samples/)
  assert.match(html, /href="#schedule-R-1\?project=COURSE">R-1 · Review samples<\/a>/)
  assert.match(html, />River<\/h3>/)
  assert.match(html, /dateTime="2026-11-23T10:00:00Z"/)
  assert.match(html, /href="#task-COURSE-1">COURSE-1<\/a>/)
  assert.match(html, /No ticket issued/)
  assert.match(html, />Pause<\/button>/)
  assert.doesNotMatch(html, /Edit maintenance schedule|owner-id/)
})

test('shows authorized maintenance edits and handles a project with no schedule', () => {
  const maintenance = {
    editable: true, schedule: { ...schedule, state: 'paused', members: ['owner-id'] },
    owner_choices: [{ id: 'owner-id', name: 'River' }],
  }
  const html = content('COURSE', [], maintenance)
  assert.match(html, />Paused<\/dd>/)
  assert.match(html, /Edit maintenance schedule/)
  assert.match(html, /Cadence in hours/)
  assert.match(html, /href="#task-COURSE-1"/)
  assert.match(html, /No recurring schedules in this project/)
  assert.match(content('COURSE', [], { editable: true, schedule: null }), /This project has no maintenance schedule/)
})

test('groups schedules by owner across projects and preserves the All projects detail link', () => {
  const html = renderToStaticMarkup(React.createElement(ScheduleContents, {
    allProjects: true,
    data: {
      recurring: [
        { ...schedule, project: 'COURSE', editable: false },
        { ...schedule, id: 'R-2', title: 'Send the report', project: 'T', editable: false },
        { ...schedule, id: 'R-3', owner: 'other-id', owner_name: 'Sam', project: 'T', editable: false },
      ],
      maintenance: { editable: false, schedule: null }, project_maintenance: [],
    },
  }))
  assert.match(html, />Recurring work<\/h2>/)
  assert.equal((html.match(/>River<\/h3>/g) || []).length, 1)
  assert.match(html, />Sam<\/h3>/)
  assert.match(html, /href="#schedule-R-1\?scope=all">R-1 · Review samples<\/a>/)
  assert.match(html, /href="#schedule-R-2\?scope=all">R-2 · Send the report<\/a>/)
  assert.match(html, /<dt>Project<\/dt><dd>COURSE<\/dd>/)
  assert.match(html, /href="#task-COURSE-1">COURSE-1<\/a>/)
  assert.match(html, />Project maintenance<\/h2>/)
  assert.doesNotMatch(html, />Pause<\/button>|Cancel schedule|Your recurring work/)
})

test('shows goal review times and date-only deadlines and gives only owners the time editor', () => {
  const html = renderToStaticMarkup(React.createElement(ScheduleContents, {
    allProjects: true,
    data: {
      recurring: [{ ...schedule, reusable_ticket: { id: 12, key: 'COURSE-12' },
        last_reviewed_at: '2026-11-22T10:00:00Z' }],
      goal_reviews: [
        { ticket: 'T-4', project: 'T', owner: 'peer-id', owner_name: 'Casey',
          title: 'Review **handoffs**', review_time: '09:15', deadline: null, editable: false },
        { ticket: 'COURSE-8', project: 'COURSE', owner: 'owner-id', owner_name: 'River',
          title: 'Ship the fixture', review_time: null, deadline: '2026-12-06', editable: true,
          review_schedule: { state: 'paused', review_time: '10:00', next_at: null } },
      ],
      maintenance: { editable: false, schedule: null },
    },
  }))
  assert.match(html, />Goal reviews<\/h2>/)
  assert.match(html, /href="#task-T-4">T-4 · Review <strong>handoffs<\/strong>/)
  assert.match(html, />09:15<\/time> Pacific/)
  assert.match(html, /href="#task-COURSE-8">COURSE-8 · Ship the fixture<\/a>/)
  assert.match(html, /dateTime="2026-12-06">2026-12-06<\/time>/)
  assert.equal((html.match(/Edit review time/g) || []).length, 1)
  assert.match(html, />10:00<\/time> Pacific · Paused/)
  assert.match(html, />Resume<\/button>/)
  assert.match(html, />Cancel reminder<\/button>/)
  assert.match(html, /<h4><a href="#task-COURSE-12"><span class="ticket-id">COURSE-12<\/span> Review samples<\/a><\/h4>/)
  assert.match(html, /<dt>Last reviewed<\/dt><dd><time dateTime="2026-11-22T10:00:00Z"/)
})

test('shows per-run outcome on schedule detail and preserves the earlier run tickets', () => {
  const runs = [
    { due_at: '2026-11-23T10:00:00Z', state: 'completed', task_key: 'COURSE-12', task_id: 12,
      terminal_at: '2026-11-23T11:00:00Z', outcome: { note: 'Both checks passed', details: '**Fictional evidence**' } },
    { due_at: '2026-11-22T10:00:00Z', state: 'missed' },
    { due_at: '2026-11-21T10:00:00Z', state: 'completed', task_key: 'COURSE-1', task_id: 5 },
  ]
  const detail = renderToStaticMarkup(React.createElement(RecurringRunList, { runs, showOutcome: true }))
  assert.match(detail, /Both checks passed/)
  assert.match(detail, /<strong>Fictional evidence<\/strong>/)
  assert.match(detail, />Missed<\/strong>/)
  assert.match(detail, /href="#task-COURSE-1"><span class="ticket-id">COURSE-1<\/span>/)
  const compact = renderToStaticMarkup(React.createElement(RecurringRunList, { runs }))
  assert.doesNotMatch(compact, /Both checks passed|Fictional evidence/)
})

test('returns to both schedule pages after sign-in', () => {
  for (const [fragment, target] of [
    ['#schedules=&project=COURSE', '/schedules?project=COURSE'],
    ['#schedule-R-2', '/schedules/R-2'],
    ['#schedule-R-2?project=COURSE', '/schedules/R-2?project=COURSE'],
    ['#schedules=&scope=all', '/schedules?scope=all'],
    ['#schedule-R-2?scope=all', '/schedules/R-2?scope=all'],
  ]) {
    assert.equal(new URL(signInHref(fragment), 'https://example.test/tasks/').searchParams.get('return_to'), target)
  }
})
