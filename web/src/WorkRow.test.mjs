import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const task = {
  id: 43,
  key: 'COURSE-4',
  title: 'Review the release',
  state: 'in_progress',
  note: 'Check the rollback plan.',
  checklist_completed: 2,
  checklist_total: 5,
}

let vite
let TaskRow
let TaskPriority
before(async () => {
  vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, hmr: false, ws: false, watch: null },
  })
  TaskRow = (await vite.ssrLoadModule('/src/WorkRow.jsx')).TaskRow
  TaskPriority = (await vite.ssrLoadModule('/src/shared.jsx')).TaskPriority
})
after(() => vite?.close())

test('task rows distinguish labelled priorities by segment count and keep the detail label', () => {
  const cases = [['high', 'High', 3], ['normal', 'Normal', 2], ['low', 'Low', 1], [undefined, 'Normal', 2]]
  for (const [priority, label, segments] of cases) {
    const row = renderToStaticMarkup(React.createElement(TaskRow, { task: { ...task, priority }, owner: false }))
    assert.match(row, new RegExp(`class="task-priority-strip ${label.toLowerCase()}" role="img" aria-label="Priority ${label}" title="Priority ${label}"`))
    assert.equal([...row.matchAll(/class="task-priority-segment"/g)].length, segments)
    assert.doesNotMatch(row, />Priority (High|Normal|Low)</)
    assert.match(row, /href="#task-COURSE-4"/)
    assert.match(row, /Check the rollback plan/)
  }
  const label = renderToStaticMarkup(React.createElement(TaskPriority, { priority: 'high' }))
  assert.match(label, />Priority High</)
})

test('Summary rows show priority words and put a single waiting label before the update', () => {
  const renderSummary = record => renderToStaticMarkup(React.createElement(TaskRow, {
    task: record, owner: false, report: 'summary',
  }))
  const waiting = renderSummary({ ...task, priority: 'high', state: 'waiting',
    wait_kind: 'person', waiting_on: 'Morgan',
  })
  assert.match(waiting, /class="task-priority"><span[^>]+>Priority <\/span>High<\/span>/)
  assert.match(waiting, /class="report-waiting-line"><span class="status waiting">Waiting<\/span><span class="task-dependency"><span>on <strong>Morgan<\/strong>/)
  assert.ok(waiting.indexOf('report-waiting-line') < waiting.indexOf('Check the rollback plan.'))
  assert.doesNotMatch(waiting, /task-priority-strip/)
  assert.match(renderSummary(task), /class="report-priority-accessible">Priority Normal<\/span>/)
})

test('a source row links to a peer request using its stored ticket reference', () => {
  for (const key of ['T-31', 'COURSE-12']) {
    const row = renderToStaticMarkup(React.createElement(TaskRow, {
      task: { ...task, context: { peer_ask_ticket: key } }, owner: false,
    }))
    assert.match(row, new RegExp(`Peer request <a href="#task-${key}">`))
  }
})

test('checklist counts remain visible and the report checklist remains an accessible disclosure', () => {
  const row = renderToStaticMarkup(React.createElement(TaskRow, { task, owner: false, report: true }))
  assert.match(row, /<button[^>]+aria-expanded="false"[^>]+aria-controls="row-checklist-43"[^>]+aria-label="Checklist for COURSE-4, 2 of 5 complete"/)
  assert.match(row, /<progress[^>]+value="2"[^>]+max="5"[^>]+aria-valuetext="2 of 5 complete"/)
  assert.match(row, />2\/5</)
  assert.match(row, /Check the rollback plan/)
  assert.match(row, /id="row-checklist-43" hidden/)
  const empty = renderToStaticMarkup(React.createElement(TaskRow, {
    task: { ...task, checklist_completed: 0, checklist_total: 0 }, owner: false, report: true,
  }))
  assert.doesNotMatch(empty, /<progress|class="task-checklist"/)
})

test('Reports shows the exact receipt and prompts only the owner of an older step', () => {
  const result = {
    ...task, at: '2026-11-21T07:42:10Z', release_after_step: true,
    release_result: { status: 'released', at: '2026-11-21T07:43:11Z', release_number: 8,
      receipt_url: 'https://example.test/receipts/8' },
  }
  const show = (record, ownReport) => renderToStaticMarkup(React.createElement(TaskRow, {
    task: record, owner: false, report: true, ownReport,
  }))
  const owned = show(result, true)
  assert.match(owned, /Check the rollback plan/)
  assert.match(owned, /Step updated <span[^>]*><button[^>]*aria-expanded="false"[^>]*><time[^>]+dateTime="2026-11-21T07:42:10Z"/)
  assert.match(owned, /Released in #8<\/strong><time[^>]+dateTime="2026-11-21T07:43:11Z"/)
  assert.match(owned, /href="https:\/\/example.test\/receipts\/8">Release receipt/)
  assert.match(owned, /Review your next step/)
  assert.doesNotMatch(show(result, false), /Review your next step/)
  assert.doesNotMatch(show({ ...result, release_after_step: false }, true), /Review your next step/)
  const rolledBack = show({ ...result, release_result: { ...result.release_result, status: 'rolled_back',
    release_number: null } }, true)
  assert.match(rolledBack, /Rolled back/)
  assert.doesNotMatch(rolledBack, /Released in #/)
})
