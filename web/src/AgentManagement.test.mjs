import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

let vite
let AppDelivery

before(async () => {
  vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, ws: false, hmr: false, watch: null },
  })
  ;({ AppDelivery } = await vite.ssrLoadModule('/src/AgentManagement.jsx'))
})

after(() => vite?.close())

test('labels the exact Start without crediting the offered ticket when other work started', () => {
  const at = '2030-04-01T16:00:00Z'
  const attempt = {
    id: 42, task: 'T-1', task_id: 1, acceptance: 'accepted', carrier: 'raft_app',
    acceptance_at: at, delivery: 'delivered', started_at: at, start_event_id: 44, reason: 'ready',
  }
  function render(next_result) {
    return renderToStaticMarkup(React.createElement(AppDelivery, {
      project: 'T', agent: 'sample', data: { cues: [], latest: { ...attempt, next_result } },
    }))
  }

  const matched = render('offered')
  assert.match(matched, /<dt>Signed pickup<\/dt>/)
  assert.match(matched, /href="#event-44-log/)
  const different = render('different_work')
  assert.match(different, /<dt>Other work started<\/dt>/)
  assert.match(different, /href="#event-44-log/)
  assert.doesNotMatch(different, /Signed pickup/)
})
