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
const fixtureKey = 'taskboard.release-history.fixture'
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
    platform: 'node', bundle: true, format: 'cjs', write: false, external: ['react', './mermaidRenderer.js'],
    loader: { '.css': 'empty' },
    plugins: [{
      name: 'release-history-test-data',
      setup(plugin) {
        plugin.onResolve({ filter: /^\.\/data\.js$/ }, () => ({ path: 'data', namespace: 'release-history-fixture' }))
        plugin.onLoad({ filter: /.*/, namespace: 'release-history-fixture' }, () => ({
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

test('ticket detail keeps the current step when an older response includes release fields', () => {
  const output = detail(receipt, { ...selected, queued_for_release: true, release_number: 8 })
  assert.ok(output.includes(selected.note))
  assert.doesNotMatch(output, /Queued for release|Release number|Active release/i)
  assert.ok(!output.includes(receipt.release_id))
})

test('historical release links still appear on their Activity entry', () => {
  const event = { id: 18, task_id: 78, task_key: 'T-78', task_title: selected.title,
    actor: 'Morgan', at: receipt.at, action: 'log', label: 'Activity logged',
    note: 'Reviewed the mounted assets.', links: [{ label: 'Release receipt', url: receipt.receipt_url }] }
  fixture.pages = {
    '/activity': { data: { items: [event] } },
    '/tasks/T-78/activity': { data: { items: [event] } },
    '/notifications/inbox': { data: { items: [{ kind: 'watched', id: 18, item: event }] } },
  }
  fixture.resources['/notifications/preferences'] = { data: { task_creations: false } }
  fixture.resources['/assignee?owner=Morgan&project=T'] = {
    data: { profile: { name: 'Morgan' }, pending_reports: [], task_counts: {
      ready: 0, in_progress: 0, waiting: 0, human_review: 0, parked: 0,
    }, has_outcomes: false },
  }
  assert.ok(detail(receipt).includes(`href="${receipt.receipt_url}"`))
  const activity = render(WorkLog, { revision: 1 })
  const inbox = render(Notifications, { session: {}, verified: true, revision: 1 })
  const profile = render(AssigneePage, { owner: 'Morgan', project: 'T', revision: 1 })
  assert.match(activity, /#task-78/)
  assert.ok(activity.includes(`href="${receipt.receipt_url}"`))
  assert.match(inbox, /#task-78/)
  assert.match(profile, /<h1>Morgan/)
  for (const output of [activity, inbox, profile]) assert.ok(!output.includes(receipt.release_id))
})
