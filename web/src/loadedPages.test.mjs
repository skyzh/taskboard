import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const require = createRequire(import.meta.url)
const hookKey = Symbol.for('tasks.loadedPages.hooks')
const bundle = await build({
  entryPoints: [fileURLToPath(new URL('./data.js', import.meta.url))],
  bundle: true,
  format: 'cjs',
  platform: 'node',
  write: false,
  plugins: [{
    name: 'loaded-pages-fixture',
    setup(plugin) {
      plugin.onResolve({ filter: /^react$/ }, () => ({ path: 'react', namespace: 'fixture' }))
      plugin.onLoad({ filter: /.*/, namespace: 'fixture' }, () => ({
        contents: ['useState', 'useRef', 'useEffect'].map(name =>
          `export const ${name} = (...args) => globalThis[Symbol.for('tasks.loadedPages.hooks')].${name}(...args)`,
        ).join('\n'),
      }))
    },
  }],
})
const fixture = { exports: {} }
new Function('require', 'module', bundle.outputFiles[0].text)(require, fixture)
const { useLoadedPages } = fixture.exports

function setup(t) {
  const slots = []
  let slot = 0
  let runEffect
  const calls = []
  const responses = []
  globalThis[hookKey] = {
    useState(initial) {
      const entry = slots[slot++] ||= { value: initial }
      return [entry.value, update => { entry.value = typeof update === 'function' ? update(entry.value) : update }]
    },
    useRef(initial) {
      return slots[slot++] ||= { current: initial }
    },
    useEffect(effect, dependencies) {
      const index = slot++
      if (!slots[index] || dependencies.some((value, i) => value !== slots[index].dependencies[i])) {
        runEffect = () => {
          slots[index]?.cleanup?.()
          slots[index] = { dependencies, cleanup: effect() }
        }
      }
    },
  }
  t.after(() => { delete globalThis[hookKey] })
  t.mock.method(globalThis, 'fetch', async path => {
    calls.push(new URL(path, 'https://tasks.example.test/tasks/'))
    assert.ok(responses.length, 'Unexpected request')
    const { data, status = 200 } = responses.shift()
    return Response.json(data, { status })
  })
  function render(...args) {
    slot = 0
    const page = useLoadedPages(...args)
    if (runEffect) {
      const effect = runEffect
      runEffect = undefined
      effect()
    }
    return page
  }
  async function finish(...args) {
    render(...args)
    await new Promise(setImmediate)
    assert.equal(responses.length, 0)
    const page = render(...args)
    assert.equal(page.pending, false)
    return page
  }
  return { calls, responses, render, finish }
}

const rows = (items, next_cursor, extra) => ({ data: { items, next_cursor, ...extra } })
const error = { status: 503, data: { message: 'Try later.' } }

test('More keeps current rows and the filtered Ready cursor through loading and retry', async t => {
  const { calls, responses, render, finish } = setup(t)
  const filters = { state: 'ready', order: 'priority', dri: 'Avery', waiting_on: 'Decision', project: 'COURSE' }
  const initial = [{ id: 1 }, { id: 2 }]
  responses.push(rows(initial, 'high:504'))
  let page = await finish('/tasks', filters, 0)
  page.loadMore()
  responses.push(error)
  const pending = render('/tasks', filters, 0)
  assert.deepEqual(pending.data.items, initial)
  assert.equal(pending.loadingMore, true)
  assert.equal(pending.loadMore, null)

  page = await finish('/tasks', filters, 0)
  assert.deepEqual(page.data.items, initial)
  assert.equal(page.error, null)
  assert.equal(page.moreError.status, 503)
  page.retry()
  responses.push(rows([{ id: 3 }], null))
  page = await finish('/tasks', filters, 0)
  assert.deepEqual(page.data.items, [...initial, { id: 3 }])
  assert.equal(page.loadMore, null)
  assert.deepEqual(calls.map(url => url.searchParams.get('before')), [null, 'high:504', 'high:504'])
  for (const url of calls) {
    assert.equal(url.pathname, '/tasks/api/v1/tasks')
    assert.equal(url.searchParams.get('limit'), '20')
    for (const [key, value] of Object.entries(filters)) assert.equal(url.searchParams.get(key), value)
  }
  responses.push(rows([{ id: 4 }], null))
  page = await finish('/tasks', { ...filters, dri: 'Blake' }, 0)
  assert.deepEqual(page.data.items, [{ id: 4 }])
  assert.equal(calls.at(-1).searchParams.get('before'), null)
})

test('More keeps one Ready row when a priority change makes it cross the cursor', async t => {
  const { calls, responses, finish } = setup(t)
  const filters = { state: 'ready', order: 'priority' }
  const first = { id: 1, priority: 'high' }
  const middle = Array.from({ length: 19 }, (_, i) => ({ id: i + 2, priority: 'high' }))
  const last = { id: 21, priority: 'high' }
  const reprioritized = { ...first, priority: 'normal' }

  responses.push(rows([first, ...middle], 'high:20'))
  let page = await finish('/tasks', filters, 0)
  page.loadMore()
  responses.push(rows([last, reprioritized], null))
  page = await finish('/tasks', filters, 0)
  assert.deepEqual(page.data.items, [reprioritized, ...middle, last])
  assert.equal(calls.at(-1).searchParams.get('before'), 'high:20')

  responses.push(rows([...middle, last], 'high:21'), rows([reprioritized], null))
  page = await finish('/tasks', filters, 1)
  assert.deepEqual(page.data.items, [...middle, last, reprioritized])
  assert.deepEqual(calls.map(url => url.searchParams.get('before')), [null, 'high:20', null, 'high:21'])
})

test('More keeps one worker run when reporting moves it across the execution cursor', async t => {
  const { calls, responses, finish } = setup(t)
  const path = '/reports/workers'
  const filters = { owner: 'Avery', order: 'execution', project: 'COURSE' }
  const first = { id: 21, role: 'build', status: 'running', execution_state: 'running' }
  const middle = Array.from({ length: 19 }, (_, i) => ({ ...first, id: 20 - i }))
  const last = { ...first, id: 1 }
  const reported = { ...first, status: 'reported', execution_state: 'reported' }

  responses.push(rows([first, ...middle], '0:2'))
  let page = await finish(path, filters, 0)
  page.loadMore()
  responses.push(rows([last, reported], null))
  page = await finish(path, filters, 0)
  assert.deepEqual(page.data.items, [reported, ...middle, last])
  assert.equal(page.loadMore, null)

  responses.push(rows([...middle, last], '0:1'), rows([reported], null))
  page = await finish(path, filters, 1)
  assert.deepEqual(page.data.items, [...middle, last, reported])
  assert.deepEqual(calls.map(url => url.searchParams.get('before')), [null, '0:2', null, '0:1'])
  assert.ok(calls.every(url => url.searchParams.get('order') === 'execution' && url.searchParams.get('project') === 'COURSE'))
})

test('all assignees keep one owner when their active group changes between pages', async t => {
  const { calls, responses, finish } = setup(t)
  const path = '/reports'
  const filters = { project: 'T' }
  const avery = { owner: 'Avery', task_count: 1 }
  const blake = { owner: 'Blake', task_count: 1 }
  const casey = { owner: 'Casey', task_count: 0 }
  const idle = { ...avery, task_count: 0 }
  responses.push({ data: { items: [avery, blake], next_cursor: '0:Blake', roster: true } })
  let page = await finish(path, filters, 0)
  page.loadMore()
  responses.push({ data: { items: [idle, casey], next_cursor: null, roster: true } })
  page = await finish(path, filters, 0)
  assert.deepEqual(page.data.items, [idle, blake, casey])
  assert.equal(page.data.roster, true)
  assert.equal(page.loadMore, null)
  assert.equal(calls.at(-1).searchParams.get('before'), '0:Blake')
  assert.equal(calls.at(-1).searchParams.get('project'), 'T')
})

test('a seeded overview uses its own cursor and refreshes appended rows from a new seed', async t => {
  const { calls, responses, render, finish } = setup(t)
  const options = {
    firstPage: { items: [1], next_cursor: 12 },
    readPage: data => data.decisions,
    cursorParam: 'decisions_before',
  }
  let page = await finish('/overview', {}, 0, options)
  assert.deepEqual(page.data.items, [1])
  assert.equal(calls.length, 0)
  page.loadMore()
  responses.push({ data: { now: { items: ['other'] }, decisions: { items: [2], next_cursor: null } } })
  page = await finish('/overview', {}, 0, options)
  assert.deepEqual(page.data.items, [1, 2])
  assert.equal(calls[0].searchParams.get('decisions_before'), '12')
  assert.equal(calls[0].searchParams.has('before'), false)
  assert.deepEqual(render('/overview', {}, 1, { ...options, enabled: false }).data.items, [1, 2])
  assert.equal(calls.length, 1)
  responses.push({ data: { decisions: { items: [4], next_cursor: null } } })
  page = await finish('/overview', {}, 1, { ...options, firstPage: { items: [3], next_cursor: 25 } })
  assert.deepEqual(page.data.items, [3, 4])
  assert.equal(calls[1].searchParams.get('decisions_before'), '25')
})

test('outcome days preserve the two-day first page and append one day per More', async t => {
  const { calls, responses, finish } = setup(t)
  const options = {
    limit: 2, moreLimit: 1, cursorParam: 'before_day',
    readPage: data => ({ items: data.days, next_cursor: data.next_day }),
  }
  responses.push({ data: { days: ['2026-11-21', '2026-11-20'], next_day: '2026-11-20' } })
  let page = await finish('/outcomes/days', { dri: 'Avery' }, 0, options)
  page.loadMore()
  responses.push({ data: { days: ['2026-11-18'], next_day: null } })
  page = await finish('/outcomes/days', { dri: 'Avery' }, 0, options)
  assert.deepEqual(page.data.items, ['2026-11-21', '2026-11-20', '2026-11-18'])
  assert.deepEqual(calls.map(url => url.searchParams.get('limit')), ['2', '1'])
  assert.equal(calls[1].searchParams.get('before_day'), '2026-11-20')
  assert.equal(page.loadMore, null)
})

test('opaque Inbox cursors survive append and a failed revision refresh keeps rows', async t => {
  const { calls, responses, finish } = setup(t)
  const path = '/notifications/inbox'
  const cursor = '["2026-11-21T12:00:00Z", "request", 2]'
  const first = { kind: 'request', id: 2 }
  const second = { kind: 'watched', id: 2 }
  responses.push(rows([first], cursor))
  let page = await finish(path, {}, 0)
  page.loadMore()
  responses.push(rows([second], null))
  page = await finish(path, {}, 0)
  assert.deepEqual(page.data.items, [first, second])
  responses.push(error)
  page = await finish(path, {}, 1)
  assert.equal(page.error.status, 503)
  assert.deepEqual(page.data.items, [first, second])
  page.retry()
  responses.push(rows([first], cursor), rows([second], null))
  page = await finish(path, {}, 1)
  assert.deepEqual(page.data.items, [first, second])
  assert.deepEqual(calls.map(url => url.searchParams.get('before')), [null, cursor, null, null, cursor])
})

test('my activity keeps the source error metadata without appending repeated notices', async t => {
  const { responses, finish } = setup(t)
  const source_errors = [{ source_key: 'github:example', error: 'Source unavailable.' }]
  responses.push(rows([1], 10, { source_errors }))
  let page = await finish('/my-activity', {}, 0)
  page.loadMore()
  responses.push(rows([2], null, { source_errors }))
  page = await finish('/my-activity', {}, 0)
  assert.deepEqual(page.data, { items: [1, 2], next_cursor: null, source_errors })
})
