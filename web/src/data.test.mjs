import assert from 'node:assert/strict'
import test from 'node:test'
import { request, signOut, watchBoardRevision, watchFrontendBuild } from './data.js'

test('signs out through the mounted same-origin POST and requires confirmation', async t => {
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })
  const statuses = [204, 403]
  globalThis.fetch = async (path, options) => {
    assert.equal(path, './auth/logout')
    assert.deepEqual(options, { method: 'POST', credentials: 'same-origin' })
    return new Response(null, { status: statuses.shift() })
  }
  await signOut()
  await assert.rejects(signOut(), /Sign out failed/)
  assert.equal(statuses.length, 0)
})

test('keeps the previous board while disconnected and refreshes on recovery', async () => {
  const host = { document: globalThis.document, fetch: globalThis.fetch, window: globalThis.window }
  let tick
  const replies = ['initial', null, 'initial', 'after-write']
  const statuses = []
  let refreshes = 0
  globalThis.document = { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} }
  globalThis.window = { setInterval(callback) { tick = callback; return 1 }, clearInterval() {} }
  globalThis.fetch = async path => {
    assert.equal(path, './api/v1/revision')
    const revision = replies.shift()
    if (revision === null) throw new Error('connection lost')
    return Response.json({ revision })
  }
  const { stop } = watchBoardRevision(() => { refreshes += 1 }, status => statuses.push(status))
  try {
    await new Promise(setImmediate)
    assert.equal(refreshes, 1)
    assert.deepEqual(statuses[0], { checking: true })
    assert.match(statuses[1].checkedAt, /Z$/)
    assert.equal(statuses[1].source, 'poll')
    await tick()
    assert.equal(refreshes, 1)
    assert.deepEqual(statuses.slice(-2), [{ checking: true }, { stale: true, backendUnavailable: false, checking: false }])
    await tick()
    assert.equal(refreshes, 2)
    assert.equal(statuses.at(-1).stale, false)
    assert.equal(statuses.at(-1).checking, false)
    await tick()
    assert.equal(refreshes, 3)
    assert.equal(statuses.at(-1).stale, false)
    assert.match(statuses.at(-1).checkedAt, /Z$/)
  } finally {
    stop()
    Object.assign(globalThis, host)
  }
})

test('revision socket refreshes on change and polling resumes while it reconnects', async () => {
  const saved = { document: globalThis.document, fetch: globalThis.fetch, window: globalThis.window }
  const sockets = []
  const statuses = []
  let initialPoll
  let timer
  let polls = 0
  let refreshes = 0
  let httpRevision = '1-1'
  let httpStatus = 200
  class FixtureSocket {
    constructor(url) {
      assert.equal(url.href, 'wss://fixture.example.test/tasks/api/v1/revisions')
      this.readyState = 0
      sockets.push(this)
    }
    connect() {
      this.readyState = 1
      this.onopen()
    }
    close() {
      this.readyState = 3
      this.onclose()
    }
    sendRevision(revision) {
      this.onmessage({ data: JSON.stringify({ revision }) })
    }
  }
  globalThis.document = {
    baseURI: 'https://fixture.example.test/tasks/', visibilityState: 'visible', addEventListener() {}, removeEventListener() {},
  }
  globalThis.window = {
    location: { href: 'https://fixture.example.test/tasks/tickets/1' }, WebSocket: FixtureSocket,
    setInterval(next) { timer = next; return 1 }, clearInterval() {},
  }
  globalThis.fetch = async (path, { signal }) => {
    if (path === './api/v1/reports') return new Response(null, { status: 500 })
    assert.equal(path, './api/v1/revision')
    polls += 1
    if (polls === 1) return new Promise(resolve => { initialPoll = { resolve, signal } })
    if (httpStatus !== 200) return new Response(null, { status: httpStatus })
    if (httpRevision === null) throw new Error('connection lost')
    return Response.json({ revision: httpRevision })
  }
  const { stop, retry } = watchBoardRevision(() => { refreshes += 1 }, status => statuses.push(status), { websocket: true })
  try {
    assert.deepEqual(statuses, [{ checking: true }])
    sockets[0].connect()
    assert.equal(initialPoll.signal.aborted, true)
    assert.equal(statuses.length, 1)
    sockets[0].sendRevision('1-1')
    assert.equal(refreshes, 1)
    assert.equal(statuses.at(-1).source, 'socket')
    assert.equal(statuses.at(-1).checking, false)
    assert.match(statuses.at(-1).checkedAt, /Z$/)
    initialPoll.resolve(Response.json({ revision: 'aborted' }))
    await new Promise(setImmediate)
    assert.equal(refreshes, 1)
    assert.equal(statuses.length, 2)
    sockets[0].sendRevision('2-2')
    assert.equal(refreshes, 2)
    await timer()
    assert.equal(polls, 1)
    sockets[0].close()
    httpRevision = '3-3'
    await timer()
    assert.equal(sockets.length, 2)
    assert.equal(polls, 2)
    assert.equal(refreshes, 3)
    assert.equal(statuses.at(-1).source, 'poll')
    assert.equal(statuses.at(-1).checking, false)
    sockets[1].close()
    httpRevision = null
    await timer()
    assert.deepEqual(statuses.at(-1), { stale: true, backendUnavailable: false, checking: false })
    assert.equal(refreshes, 3)
    sockets[2].connect()
    sockets[2].sendRevision('3-3')
    assert.equal(statuses.at(-1).source, 'socket')
    assert.equal(statuses.at(-1).stale, false)
    assert.equal(statuses.at(-1).checking, false)
    assert.equal(refreshes, 4)

    httpStatus = 503
    await assert.rejects(request('/reports'), /Taskboard could not complete this request/)
    await new Promise(setImmediate)
    assert.equal(statuses.at(-1).backendUnavailable, true)
    sockets[2].sendRevision('4-4')
    assert.equal(statuses.at(-1).backendUnavailable, true)
    assert.equal(refreshes, 4)
    httpStatus = 200
    httpRevision = '4-4'
    await retry()
    assert.equal(statuses.at(-1).backendUnavailable, false)
    assert.equal(refreshes, 5)

    assert.equal(window.location.href, 'https://fixture.example.test/tasks/tickets/1')
  } finally {
    stop()
    Object.assign(globalThis, saved)
  }
})

test('checks the loaded frontend build, waits for edits, and reloads the same route when safe', async t => {
  const host = { document: globalThis.document, fetch: globalThis.fetch, window: globalThis.window }
  t.after(() => Object.assign(globalThis, host))
  const initial = 'a'.repeat(64)
  const changed = 'b'.repeat(64)
  const requests = []
  const updates = []
  const reloads = []
  let tick
  let visibilityChanged
  let finishHiddenRequest
  const hiddenRequest = new Promise(resolve => { finishHiddenRequest = resolve })
  const replies = [
    Response.json({ build: initial }),
    new Error('connection lost'),
    Response.json({ build: 'invalid response' }),
    Response.json({ build: changed }),
    Response.json({ build: initial }),
    Response.json({ build: changed }),
    hiddenRequest,
    Response.json({ build: changed }),
  ]
  let editing = false
  globalThis.document = {
    visibilityState: 'visible',
    addEventListener(event, listener) {
      assert.equal(event, 'visibilitychange')
      visibilityChanged = listener
    },
    removeEventListener(event, listener) {
      assert.equal(event, 'visibilitychange')
      assert.equal(visibilityChanged, listener)
    },
    querySelector(selector) {
      if (selector === 'meta[name="tasks-frontend-build"]') return { content: initial }
      if (selector === '[data-reload-blocked="true"]') return editing ? {} : null
      throw new Error(`Unexpected selector ${selector}`)
    },
  }
  const route = 'https://board.example.test/tasks/?view=shared#task-43'
  globalThis.window = {
    location: { href: route, reload(...args) { reloads.push([this.href, ...args]) } },
    setInterval(callback, interval) {
      assert.equal(interval, 5_000)
      tick = callback
      return 1
    },
    clearInterval(timer) { assert.equal(timer, 1) },
  }
  globalThis.fetch = async (path, options) => {
    requests.push({ path, ...options })
    const response = replies.shift()
    if (response instanceof Error) throw response
    return response
  }
  const stop = watchFrontendBuild(value => updates.push(value))
  try {
    await new Promise(setImmediate)
    assert.deepEqual(updates, [false])
    await tick()
    await tick()
    assert.deepEqual(updates, [false])
    assert.deepEqual(reloads, [])
    editing = true
    await tick()
    await tick()
    await tick()
    assert.deepEqual(updates, [false, true, false, true])
    assert.deepEqual(reloads, [])

    editing = false
    const inFlight = tick()
    document.visibilityState = 'hidden'
    visibilityChanged()
    await tick()
    assert.equal(requests.length, 7)
    assert.equal(requests.at(-1).signal.aborted, true)
    finishHiddenRequest(Response.json({ build: changed }))
    await inFlight
    assert.deepEqual(reloads, [])

    document.visibilityState = 'visible'
    visibilityChanged()
    await new Promise(setImmediate)
    await tick()
    assert.deepEqual(reloads, [[route]])
    assert.equal(replies.length, 0)
    for (const { path, cache, credentials, method } of requests) {
      assert.deepEqual({ path, cache, credentials, method }, {
        path: './api/v1/frontend-build', cache: 'no-store', credentials: 'same-origin', method: 'GET',
      })
    }
  } finally {
    stop()
  }
})

test('frontend reload waits for concurrent mutations after navigation removes their controls', async t => {
  const host = { document: globalThis.document, fetch: globalThis.fetch, window: globalThis.window }
  t.after(() => Object.assign(globalThis, host))
  const updates = []
  let reloads = 0
  let tick
  let finishWatch
  let finishHandling
  let finishRead
  const watchBody = new Promise(resolve => { finishWatch = resolve })
  const handlingResponse = new Promise(resolve => { finishHandling = resolve })
  const readResponse = new Promise(resolve => { finishRead = resolve })
  globalThis.document = {
    visibilityState: 'visible',
    addEventListener() {},
    removeEventListener() {},
    querySelector(selector) {
      return selector === 'meta[name="tasks-frontend-build"]' ? { content: 'a'.repeat(64) } : null
    },
  }
  globalThis.window = {
    location: { reload() { reloads += 1 } },
    setInterval(callback) { tick = callback },
    clearInterval() {},
  }
  globalThis.fetch = async path => {
    switch (path) {
      case './api/v1/tasks/43/watch': return { status: 200, ok: true, json: () => watchBody }
      case './api/v1/notifications/requests/9/handled': return handlingResponse
      case './api/v1/reports': return readResponse
      case './api/v1/frontend-build': return Response.json({ build: 'b'.repeat(64) })
      default: throw new Error(`Unexpected path ${path}`)
    }
  }
  const watch = request('/tasks/43/watch', { body: { watch: true } })
  const handling = request('/notifications/requests/9/handled', { method: 'POST' })
  const read = request('/reports')
  const stop = watchFrontendBuild(value => updates.push(value))
  try {
    await new Promise(setImmediate)
    assert.deepEqual(updates, [true])
    assert.equal(reloads, 0)

    finishHandling(Response.json({ error: 'Write failed' }, { status: 503 }))
    await assert.rejects(handling, /Write failed/)
    await tick()
    assert.deepEqual(updates, [true, true])
    assert.equal(reloads, 0)

    finishWatch({ watching: true })
    await watch
    await tick()
    assert.equal(reloads, 1)

    finishRead(Response.json({ items: [] }))
    await read
  } finally {
    stop()
  }
})
