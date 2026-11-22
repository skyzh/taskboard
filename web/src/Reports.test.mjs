import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

let vite
let AssigneeReport
let DriWorkers
let relativeAge
let watchReportTime
let TimeZoneContext
let createTimeZoneFormatters
before(async () => {
  vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, hmr: false, ws: false, watch: null },
  })
  const reportModule = await vite.ssrLoadModule('/src/Reports.jsx')
  AssigneeReport = reportModule.AssigneeReport
  DriWorkers = (await vite.ssrLoadModule('/src/DriWorkers.jsx')).default
  const reportTime = await vite.ssrLoadModule('/src/ReportTime.jsx')
  relativeAge = reportTime.relativeAge
  watchReportTime = reportTime.watchReportTime
  ;({ TimeZoneContext, createTimeZoneFormatters } = await vite.ssrLoadModule('/src/timeZone.js'))
})
after(() => vite?.close())

test('Reports lead with progress and link to goals, outcomes and exact activity times', t => {
  const at = '2026-11-20T21:00:00Z'
  const stepAt = '2026-11-21T05:40:00Z'
  const activityAt = '2026-11-21T06:55:00Z'
  const reportedAt = '2026-11-21T06:50:00Z'
  t.mock.method(Date, 'now', () => Date.parse('2026-11-21T06:56:00Z'))
  const unblock = 'Nora approves the signed release packet.\nThe post-restart service health check must be green.'
  const step = 'Compare the final two rows before publishing.'
  const multiline = 'Confirm the duplicate rows against the intake record.\nThen send the signed count to Nora.'
  const longer = 'Compare the checksum of the newly exported settlement file with the digest in the signed intake record, then attach the verification receipt before the release owner can approve it.'
  const release = 'The package checks passed, and the receipts are attached.\n\nDo not deploy until Nora approves the signed proof and the service health check is green.'
  const links = [{ label: 'Original receipt', url: 'https://example.test/receipt' }]
  const entry = {
    owner: 'Avery',
    last_activity: { id: 35, at: activityAt },
    goals: [{ kind: 'standing', summary: 'Make reports clear.' }],
    tasks: [
      {
        id: 11, key: 'COURSE-11', title: 'Import a sample', at, state: 'waiting', waiting_on: 'Nora',
        note: unblock, summary_label: 'Unblock condition', event_id: 31, links,
      },
      {
        id: 12, title: 'Prepare the rollout', at, state: 'in_progress', action: 'update',
        note: `Prepare the rollout. ${release}`, summary_label: 'Current step',
        checklist_completed: 1, checklist_total: 2, event_id: 32, links,
      },
      {
        id: 14, title: 'Compare the export', at: stepAt, last_activity_at: activityAt,
        state: 'in_progress', action: 'update', note: step, summary_label: 'Current step', event_id: 35, links,
      },
      {
        id: 15, title: 'Verify the signed count', at, state: 'in_progress', action: 'update',
        note: multiline, summary_label: 'Current step', event_id: 36, links,
      },
      {
        id: 16, title: 'Verify settlement checksum', at, state: 'in_progress', action: 'update',
        note: longer, summary_label: 'Current step', event_id: 37, links,
      },
    ],
    task_count: 5,
    task_counts: { human_review: 0, waiting: 1, in_progress: 4, ready: 27 },
    recent_outcomes: [
      {
        id: 33, task_id: 13, task_key: 'COURSE-13', task_title: 'Choose the export format', at, actor: 'Sam',
        action: 'decide', label: 'Decision recorded', context: { choice: 'CSV' },
        note: 'Full decision record from the original author.', links,
      },
    ],
    report_count: 1,
    pending_reports: [
      {
        run_id: 3, task_id: 12, task_title: 'Prepare the rollout', reported_at: reportedAt,
        worker: 'Blake', result: 'blocked', event_id: 34,
        report: 'Full worker report from the original author.', links,
      },
    ],
  }
  const zone = createTimeZoneFormatters('America/Los_Angeles')
  const html = renderToStaticMarkup(React.createElement(TimeZoneContext.Provider, { value: zone },
    React.createElement(AssigneeReport, { entry, project: 'COURSE' })))
  const primary = html.replaceAll(/<details\b[^>]*>[\s\S]*?<\/details>/g, '')

  assert.match(primary, /href="#task-COURSE-11"[^>]*>.*Import a sample/)
  assert.match(primary, /href="#task-COURSE-13"[^>]*>.*Choose the export format/)
  assert.match(primary, /Waiting on <strong>Nora<\/strong>/)
  assert.ok(primary.includes(unblock))
  assert.ok(primary.includes(release.replace('\n\n', '</p><p>')))
  assert.ok(primary.includes(step))
  assert.ok(primary.includes(multiline))
  assert.ok(primary.includes(longer))
  const exact = zone.formatTimestamp(activityAt)
  assert.ok(primary.includes(`href="#event-35-log" aria-label="Last activity, ${exact}"><time dateTime="${activityAt}" title="${exact}">1m ago</time>`))
  assert.ok(primary.includes(`Reported <time dateTime="${reportedAt}" title="${zone.formatTimestamp(reportedAt)}">6m ago</time>`))
  assert.ok(!primary.includes(stepAt))
  assert.match(primary, /Checklist for T-12, 1 of 2 complete/)
  assert.match(primary, /aria-label="Decision recorded"/)
  assert.match(primary, /Choose the export format<\/a> · CSV/)
  assert.match(primary, /href="#event-33-log" aria-label="Open outcome for COURSE-13,/)
  assert.match(primary, /<time datetime="2026-11-20T21:00:00Z"/i)
  assert.match(primary, /href="#dri=Avery&amp;section=ready-title&amp;project=COURSE">Ready <strong>27<\/strong>/)
  assert.match(primary, /href="#event-34-log"/)
  assert.doesNotMatch(primary, /human_review-title|waiting-title|now-title|example\.test|Current step|Unblock condition|>Waiting<|>In progress</)
  assert.doesNotMatch(html, /Full decision record|Update and evidence for COURSE-11/)
  assert.match(html, /Full worker report from the original author/)
  assert.doesNotMatch(primary, /Full worker report/)
  assert.equal(html.split(unblock).length, 2)
  assert.equal(html.split(release.replace('\n\n', '</p><p>')).length, 2)
  assert.equal(html.split(step).length, 2)
  assert.match(html, /Current work<\/h3>[\s\S]*?<details class="report-owner-section report-goals-panel"><summary>Goals<\/summary>/)
  assert.match(html, /href="#dri=Avery&amp;section=dri-standing-goal-title&amp;project=COURSE">Make reports clear\./)
})

test('Reports leave legacy receipts and preceding narratives on the ticket while showing authored actions', () => {
  const links = [{ label: 'Original proof', url: 'https://example.test/proof' }]
  const receipt = `Review exact 4-file ${'ab'.repeat(32)} in /fixtures/taskboard/release-candidate. The TLS probe reused the original browser and agent CLI sessions across two actual service processes, with no new login. The fixture test suite passed. Absolute expiry, logout, audience scope and private file modes are covered.`
  const narrative = 'The fixture and recorded restart validation are complete.'
  const next = 'Confirm session persistence.\n\nDo not deploy until the health check is green.'
  const details = 'Private file modes and logout validation are documented here.'
  const tasks = [
    { id: 40, title: 'Review durable sessions', state: 'waiting', waiting_on: 'Morgan',
      note: receipt, summary_label: 'Unblock condition', event_id: 140, links },
    { id: 41, title: 'Confirm session persistence', state: 'in_progress',
      note: `${narrative}\n\nNext:\n${next}`, summary_label: 'Current step', event_id: 141, links },
    { id: 42, title: 'Verify the signed proof', state: 'in_progress',
      note: 'Check the signed proof.', context: { activity_details: details },
      summary_label: 'Current step', event_id: 142, links },
  ]
  const entry = { owner: 'Avery', tasks, task_count: tasks.length, pending_reports: [], report_count: 0 }
  const html = renderToStaticMarkup(React.createElement(AssigneeReport, { entry, project: 'COURSE' }))
  const primary = html.replaceAll(/<details\b[^>]*>[\s\S]*?<\/details>/g, '')
  assert.match(primary, /Waiting on <strong>Morgan<\/strong>/)
  assert.doesNotMatch(primary, /Review exact|release-candidate|Original proof/)
  assert.doesNotMatch(primary, /<strong>Next step<\/strong>/)
  assert.ok(primary.includes(next.replace('\n\n', '</p><p>')))
  assert.ok(primary.includes('Check the signed proof.'))
  assert.equal(html.split(next.replace('\n\n', '</p><p>')).length, 2)
  for (const text of [receipt, narrative, details]) {
    assert.ok(!html.includes(text))
  }
  for (const id of [40, 41, 42]) assert.match(html, new RegExp(`href="#task-${id}"`))
  assert.doesNotMatch(html, /https:\/\/example\.test\/proof/)
})

test('Reports leaves the recorded worker count on the DRI view', () => {
  const counts = { recorded: 3, running: 1, running_reviews: 1, launched: 1, launched_reviews: 2 }
  const entry = { owner: 'Avery', tasks: [], task_count: 0, pending_reports: [], report_count: 0, worker_counts: counts }
  const reports = renderToStaticMarkup(React.createElement(AssigneeReport, { entry }))
  assert.doesNotMatch(reports, /Recorded worker runs|2 running|3 started/)

  const workers = renderToStaticMarkup(React.createElement(DriWorkers, { owner: 'Avery', counts }))
  assert.match(workers, /Recorded worker runs · 2 running · 3 started/)
})

test('one visible clock updates ages and resumes with the current time after hiding', t => {
  const at = '2026-11-21T06:55:00Z'
  const globals = { document: globalThis.document, window: globalThis.window }
  let now = Date.parse(at) + 45_000
  let tick
  const ages = []
  t.mock.method(Date, 'now', () => now)
  const document = Object.assign(new EventTarget(), { visibilityState: 'visible' })
  globalThis.document = document
  globalThis.window = {
    setInterval(callback, delay) {
      assert.equal(tick, undefined)
      assert.equal(delay, 15_000)
      tick = callback
      return 1
    },
    clearInterval(id) { if (id === 1) tick = undefined },
  }
  const stop = watchReportTime(time => ages.push(relativeAge(at, time)))
  try {
    now += 15_000
    tick()
    assert.deepEqual(ages, ['just now', '1m ago'])
    document.visibilityState = 'hidden'
    document.dispatchEvent(new Event('visibilitychange'))
    assert.equal(tick, undefined)
    now += 60_000
    document.visibilityState = 'visible'
    document.dispatchEvent(new Event('visibilitychange'))
    assert.deepEqual(ages, ['just now', '1m ago', '2m ago'])
  } finally {
    stop()
    Object.assign(globalThis, globals)
  }
  assert.equal(tick, undefined)
})
