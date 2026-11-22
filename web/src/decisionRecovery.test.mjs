import assert from 'node:assert/strict'
import test from 'node:test'
import { isUncertainDecision, watchDecision } from './decisionRecovery.js'

const afterRequests = () => new Promise(setImmediate)
const path = '/tasks/COURSE-7/decision/41'

test('decision verification waits between failed reads and stops on a recovered receipt', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const requests = []
  const receipts = []
  const answer = { event_id: 42, at: '2026-11-21T07:59:00Z', note: 'Recorded answer', choice: null }
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({ url, ...options })
    if (requests.length === 1) return Response.json({ error: 'Bad Gateway' }, { status: 502 })
    if (requests.length === 2) throw new TypeError('Network error')
    return Response.json({ status: 'answered', answer })
  })
  const stop = watchDecision(path, receipt => receipts.push(receipt))
  try {
    await afterRequests()
    t.mock.timers.tick(4_999)
    assert.equal(requests.length, 1)
    t.mock.timers.tick(1)
    await afterRequests()
    assert.equal(requests.length, 2)
    assert.deepEqual(receipts, [])
    t.mock.timers.tick(5_000)
    await afterRequests()
    assert.deepEqual(receipts, [{ status: 'answered', answer }])
    t.mock.timers.tick(60_000)
    assert.equal(requests.length, 3)
    for (const { url, cache, method, credentials } of requests) {
      assert.deepEqual({ url, cache, method, credentials }, {
        url: './api/v1' + path, cache: 'no-store', method: 'GET', credentials: 'same-origin',
      })
    }
  } finally {
    stop()
  }
})

test('a stalled decision check times out and cleanup cancels the active read', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const signals = []
  const receipts = []
  t.mock.method(globalThis, 'fetch', (_url, { signal }) => {
    signals.push(signal)
    return new Promise((resolve, reject) => {
      signal.addEventListener('abort', () => reject(signal.reason), { once: true })
    })
  })
  const stop = watchDecision(path, receipt => receipts.push(receipt))
  t.after(stop)
  t.mock.timers.tick(14_999)
  assert.equal(signals.length, 1)
  assert.equal(signals[0].aborted, false)
  t.mock.timers.tick(1)
  await afterRequests()
  assert.equal(signals[0].aborted, true)
  t.mock.timers.tick(5_000)
  assert.equal(signals.length, 2)
  stop()
  assert.equal(signals[1].aborted, true)
  await afterRequests()
  t.mock.timers.tick(60_000)
  assert.equal(signals.length, 2)
  assert.deepEqual(receipts, [])
})

test('a denied exact receipt never becomes an open decision', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const receipts = []
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({ error: 'Not found.' }, { status: 404 }))
  const stop = watchDecision(path, receipt => receipts.push(receipt))
  t.after(stop)
  await afterRequests()
  assert.deepEqual(receipts, [{ status: 'unavailable' }])
  t.mock.timers.tick(60_000)
  assert.equal(fetch.mock.callCount(), 1)
})

test('a conflicting open receipt can be checked again without an immediate loop', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const statuses = []
  let calls = 0
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({
    status: ++calls === 1 ? 'open' : 'closed', answer: null,
  }))
  const stop = watchDecision(path, ({ status }) => {
    statuses.push(status)
    return status !== 'open'
  })
  t.after(stop)
  await afterRequests()
  t.mock.timers.tick(4_999)
  assert.deepEqual(statuses, ['open'])
  assert.equal(fetch.mock.callCount(), 1)
  t.mock.timers.tick(1)
  await afterRequests()
  assert.deepEqual(statuses, ['open', 'closed'])
})

test('validation and permission errors are definitive, and timeouts or stale requests are checked', () => {
  for (const status of [undefined, 408, 409, 429, 500, 502, 504]) {
    assert.equal(isUncertainDecision({ status }), true)
  }
  for (const status of [400, 401, 403, 404]) {
    assert.equal(isUncertainDecision({ status }), false)
  }
})
