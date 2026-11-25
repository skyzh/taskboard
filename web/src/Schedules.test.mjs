import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

let vite
let ScheduleContents
let signInHref

before(async () => {
  vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, ws: false, hmr: false, watch: null },
  })
  ;({ ScheduleContents } = await vite.ssrLoadModule('/src/Schedules.jsx'))
  ;({ signInHref } = await vite.ssrLoadModule('/src/shared.jsx'))
})

after(() => vite?.close())

const schedule = {
  id: 'R-1', title: 'Review samples', owner: 'owner-id', owner_name: 'River',
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

test('renders only own recurring work and contextual links to issued tickets', () => {
  const html = content('COURSE', [schedule], { editable: false, schedule: null })
  assert.match(html, /Review samples/)
  assert.match(html, /href="#schedule-R-1\?project=COURSE">R-1 · Review samples<\/a>/)
  assert.match(html, />River<\/dd>/)
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
  assert.match(html, /You have no recurring schedules in this project/)
  assert.match(content('COURSE', [], { editable: true, schedule: null }), /This project has no maintenance schedule/)
})

test('returns to both schedule pages after sign-in', () => {
  for (const fragment of ['#schedules=&project=COURSE', '#schedule-R-2', '#schedule-R-2?project=COURSE']) {
    assert.equal(new URL(signInHref(fragment), 'https://example.test/tasks/').searchParams.get('fragment'), fragment)
  }
  assert.equal(signInHref('#schedule-R-2?project=COURSE&other=true'), './auth/login')
})
