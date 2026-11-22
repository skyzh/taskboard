import assert from 'node:assert/strict'
import { test } from 'node:test'
import { watchBoardSections } from './sectionIndex.js'

function board(t, { scrollHeight = 1950 } = {}) {
  const positions = {
    'queue-heading': 140,
    'now-title': 560,
    'decisions-title': 900,
    'outcomes-title': 1120,
    'workstreams-title': 1600,
  }
  const absent = new Set()
  const changes = []
  const frames = new Map()
  const observed = new Set()
  const content = {}
  let nextFrame = 0
  let headerBottom = 150
  const header = { getBoundingClientRect: () => ({ bottom: headerBottom }) }
  const main = { querySelector: () => content }
  const scrollport = { scrollTop: 0, clientHeight: 600, scrollHeight }
  const headings = Object.fromEntries(Object.keys(positions).map(id => {
    const section = {}
    return [id, {
      id,
      closest: () => section,
      getBoundingClientRect: () => ({
        top: positions[id] - scrollport.scrollTop,
        height: absent.has(id) ? 0 : 24,
      }),
    }]
  }))
  const nav = {
    closest: () => header,
    querySelectorAll: () => Object.keys(headings).map(id => ({ hash: `#${id}` })),
  }
  const window = Object.assign(new EventTarget(), {
    getComputedStyle: () => ({ scrollMarginTop: '26px' }),
    requestAnimationFrame(callback) {
      frames.set(++nextFrame, callback)
      return nextFrame
    },
    cancelAnimationFrame(id) { frames.delete(id) },
  })
  let resize
  class ResizeObserver {
    constructor(callback) { resize = callback }
    observe(node) { observed.add(node) }
    disconnect() { observed.clear() }
  }
  const globals = { window, ResizeObserver, document: {
    querySelector: () => main,
    getElementById: id => headings[id],
    scrollingElement: scrollport,
  } }
  const previous = Object.fromEntries(Object.keys(globals).map(key => [key, globalThis[key]]))
  Object.assign(globalThis, globals)
  const stop = watchBoardSections(nav, id => changes.push(id))
  t.after(() => {
    stop()
    Object.assign(globalThis, previous)
  })

  return {
    changes, positions, absent, observed, content, frames, stop,
    scrollTo(value) {
      scrollport.scrollTop = value
      window.dispatchEvent(new Event('scroll'))
    },
    layoutChanged() { resize() },
    headerResized(bottom) {
      headerBottom = bottom
      window.dispatchEvent(new Event('resize'))
    },
    flush() {
      const pending = [...frames.values()]
      frames.clear()
      for (const callback of pending) callback()
    },
  }
}

test('window scrolling updates the section and reaches a short last section at the bottom', t => {
  const fixture = board(t)
  assert.deepEqual(fixture.changes, ['queue-heading'])
  fixture.scrollTo(400)
  fixture.scrollTo(410)
  assert.equal(fixture.frames.size, 1)
  fixture.flush()
  fixture.scrollTo(735)
  fixture.flush()
  fixture.scrollTo(960)
  fixture.flush()
  fixture.scrollTo(1350)
  fixture.flush()
  fixture.scrollTo(1250)
  fixture.flush()
  assert.deepEqual(fixture.changes, [
    'queue-heading', 'now-title', 'decisions-title', 'outcomes-title', 'workstreams-title', 'outcomes-title',
  ])
})

test('the section index reacts to content shifts and stops observing after cleanup', t => {
  const fixture = board(t)
  assert.ok(fixture.observed.has(fixture.content))
  fixture.scrollTo(384.5)
  fixture.flush()
  assert.deepEqual(fixture.changes, ['queue-heading', 'now-title'])
  fixture.positions['decisions-title'] = 550
  fixture.layoutChanged()
  fixture.flush()
  assert.equal(fixture.changes.at(-1), 'decisions-title')
  fixture.scrollTo(600)
  fixture.stop()
  assert.equal(fixture.frames.size, 0)
  assert.equal(fixture.observed.size, 0)
  fixture.scrollTo(1000)
  assert.equal(fixture.frames.size, 0)
})

test('resize accounts for the header and all-visible pages do not default to the last section', t => {
  const fixture = board(t, { scrollHeight: 600 })
  fixture.positions['now-title'] = 205
  fixture.positions['decisions-title'] = 300
  fixture.positions['outcomes-title'] = 400
  fixture.positions['workstreams-title'] = 500
  fixture.headerResized(210)
  fixture.flush()
  assert.deepEqual(fixture.changes, ['queue-heading', 'now-title'])
  fixture.absent.add('now-title')
  fixture.layoutChanged()
  fixture.flush()
  assert.equal(fixture.changes.at(-1), 'queue-heading')
})
