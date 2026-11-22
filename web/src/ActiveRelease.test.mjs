import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const require = createRequire(import.meta.url)
const sourceDirectory = fileURLToPath(new URL('.', import.meta.url))
const fixture = { resources: {}, pages: {} }
const fixtureKey = 'taskboard.active-release.fixture'
const selected = {
  id: 78, key: 'T-78', title: 'Review the rollout', state: 'in_progress',
  summary_label: 'Current step', note: 'Compare the older build with the mounted assets.',
  dri: 'Morgan', links: [],
}
const receipt = {
  status: 'released', active: 'a'.repeat(64), release_id: 'fixture-release-8',
  release_number: 8, at: '2026-11-21T14:00:00Z', receipt_url: 'https://example.test/releases/fixture-8',
}
const render = (Component, props) => renderToStaticMarkup(React.createElement(Component, props))
let WorkLog
let TaskDetail
let Notifications
let AssigneePage

before(async () => {
  globalThis[Symbol.for(fixtureKey)] = fixture
  const result = await build({
    stdin: {
      contents: `
        export { WorkLog, TaskDetail } from './Work.jsx'
        export { default as Notifications } from './Notifications.jsx'
        export { default as AssigneePage } from './AssigneePage.jsx'
      `,
      resolveDir: sourceDirectory,
    },
    platform: 'node', bundle: true, format: 'cjs', write: false, external: ['react'],
    loader: { '.css': 'empty' },
    plugins: [{
      name: 'active-release-test-data',
      setup(plugin) {
        plugin.onResolve({ filter: /^\.\/data\.js$/ }, () => ({ path: 'data', namespace: 'active-release-fixture' }))
        plugin.onLoad({ filter: /.*/, namespace: 'active-release-fixture' }, () => ({
          resolveDir: sourceDirectory,
          contents: `
            export * from ${JSON.stringify(fileURLToPath(new URL('./data.js', import.meta.url)))}
            const fixture = globalThis[Symbol.for(${JSON.stringify(fixtureKey)})]
            export const useResource = path => fixture.resources[path] || {}
            export const useLoadedPages = path => fixture.pages[path] || {}
          `,
        }))
      },
    }],
  })
  const module = { exports: {} }
  new Function('require', 'module', result.outputFiles[0].text)(require, module)
  ;({ WorkLog, TaskDetail, Notifications, AssigneePage } = module.exports)
})

after(() => { delete globalThis[Symbol.for(fixtureKey)] })

function detail(activeRelease, task = selected) {
  fixture.resources['/tasks/' + task.key] = {
    data: { task, active_release: activeRelease, checklist: [], workers: [], sources: [] },
  }
  return render(TaskDetail, { id: task.key, session: {} })
}

test('the exact verified receipt and the original next step appear together', () => {
  const output = detail(receipt)
  const focus = output.match(/<section[^>]+aria-label="Current step"[^>]*>.*?<\/section>/s)?.[0]
  assert.ok(focus)
  assert.match(focus, /Active release/i)
  assert.ok(focus.includes(receipt.active))
  assert.ok(focus.includes(receipt.release_id))
  assert.ok(focus.includes(`href="${receipt.receipt_url}"`))
  assert.ok(focus.includes(selected.note))

  const planned = detail(null, { ...selected, queued_for_release: true, release_number: null })
  assert.match(planned, /Queued for release/)
  assert.doesNotMatch(planned, /Active release/i)
  assert.ok(planned.includes(selected.note))
})

test('a rollback displays the restored digest and an unverified result makes no active claim', () => {
  const restored = { ...receipt, status: 'rolled_back', active: 'b'.repeat(64),
    release_id: 'fixture-rollback', release_number: null, receipt_url: 'https://example.test/releases/rollback' }
  const rollback = detail(restored)
  assert.match(rollback, /Active release/i)
  assert.ok(rollback.includes(restored.active))
  assert.ok(rollback.includes(restored.release_id))
  assert.ok(rollback.includes(`href="${restored.receipt_url}"`))
  assert.ok(!rollback.includes(receipt.active))
  assert.ok(rollback.includes(selected.note))

  const failed = detail({ ...restored, status: 'rollback_failed', active: null, receipt_url: null })
  assert.match(failed, /Active release (?:is )?(?:unverified|not verified)/i)
  assert.ok(failed.includes(restored.release_id))
  assert.ok(!failed.includes(restored.active))
  assert.ok(!failed.includes(restored.receipt_url))
  assert.ok(failed.includes(selected.note))
})

test('Activity, the Inbox and the assignee profile render without pulling in the ticket receipt', () => {
  const event = { id: 18, task_id: 78, task_key: 'T-78', task_title: selected.title,
    actor: 'Morgan', at: receipt.at, action: 'log', label: 'Activity logged',
    note: 'Reviewed the mounted assets.', links: [] }
  fixture.pages = {
    '/activity': { data: { items: [event] } },
    '/notifications/inbox': { data: { items: [{ kind: 'watched', id: 18, item: event }] } },
  }
  fixture.resources['/notifications/preferences'] = { data: { task_creations: false } }
  fixture.resources['/assignee?owner=Morgan&project=T'] = {
    data: { profile: { name: 'Morgan' }, pending_reports: [], task_counts: {
      ready: 0, in_progress: 0, waiting: 0, human_review: 0, parked: 0,
    }, has_outcomes: false },
  }
  assert.ok(detail(receipt).includes(receipt.release_id))
  const activity = render(WorkLog, { revision: 1 })
  const inbox = render(Notifications, { session: {}, verified: true, revision: 1 })
  const profile = render(AssigneePage, { owner: 'Morgan', project: 'T', revision: 1 })
  assert.match(activity, /#task-78/)
  assert.match(inbox, /#task-78/)
  assert.match(profile, /<h1>Morgan/)
  for (const output of [activity, inbox, profile]) assert.ok(!output.includes(receipt.release_id))
})
