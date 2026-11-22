import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const task = (id, title, state = 'ready') => ({ id, title, dri: 'Alex', state })
const seed = task(1, 'Prepare the workspace', 'done')
const base = task(2, 'Publish contracts', 'in_progress')
const parser = task(3, 'Build the parser')
const writer = task(4, 'Build the writer')
const release = task(5, 'Package the release')
const items = [
  { task: release, workstream: 'Release', prerequisites: [base, writer] },
  { task: writer, workstream: 'Output', prerequisites: [base] },
  { task: parser, workstream: 'Core', prerequisites: [base] },
  { task: base, workstream: 'Core', prerequisites: [seed] },
]
const taskHref = id => `#task-${id}`

let vite
let module
let shared
before(async () => {
  vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, hmr: false, watch: null },
  })
  module = await vite.ssrLoadModule('/src/MilestoneMap.jsx')
  shared = await vite.ssrLoadModule('/src/shared.jsx')
})
after(() => vite?.close())

test('loads each milestone page from the app-relative API', async t => {
  const signal = new AbortController().signal
  const pages = [
    { items: items.slice(0, 2), next_cursor: 4 },
    { items: items.slice(2), next_cursor: null },
  ]
  const requests = []
  t.mock.method(globalThis, 'fetch', async (path, options) => {
    assert.equal(options.signal, signal)
    assert.equal(options.credentials, 'same-origin')
    const url = new URL(path, 'http://tasks.test/tasks/')
    requests.push(url.pathname + url.search)
    return Response.json(pages.shift())
  })
  assert.deepEqual(await module.loadMilestones(signal), items)
  assert.deepEqual(requests, [
    '/tasks/api/v1/milestones?limit=20',
    '/tasks/api/v1/milestones?limit=20&before=4',
  ])
})

test('places every prerequisite before its dependents and keeps all saved arrows', () => {
  const { columns, edges } = module.buildMilestoneGraph(items)
  assert.deepEqual(columns.map(column => column.map(({ task }) => task.id)), [[1], [2], [3, 4], [5]])
  assert.deepEqual(edges.map(({ key }) => key).sort(), ['1-2', '2-3', '2-4', '2-5', '4-5'])
  assert.equal(edges.find(({ key }) => key === '2-4').source.workstream, 'Core')
  assert.equal(edges.find(({ key }) => key === '2-4').target.workstream, 'Output')
  assert.equal(edges.find(({ key }) => key === '2-5').track, 0)
})

test('draws one arrow from each prerequisite card to its named relationship row', () => {
  const { columns, edges } = module.buildMilestoneGraph(items)
  const frame = { left: 50, top: 40, width: 1000, height: 450 }
  const cards = new Map()
  const rows = new Map()
  for (const column of columns) {
    column.forEach((node, row) => {
      const left = frame.left + 24 + node.column * 240
      const top = frame.top + 24 + row * 180
      cards.set(node.task.id, { left, right: left + 184, top })
      node.incoming.forEach(({ key }, index) => {
        rows.set(key, { top: top + 60 + index * 25, height: 20 })
      })
    })
  }
  const diagram = {
    getBoundingClientRect: () => frame,
    querySelectorAll: selector => selector === '[data-milestone-id]'
      ? [...cards].map(([id, rect]) => ({ dataset: { milestoneId: String(id) }, getBoundingClientRect: () => rect }))
      : [...rows].map(([edge, rect]) => ({ dataset: { milestoneEdge: edge }, getBoundingClientRect: () => rect })),
  }
  const lines = []
  const tips = []
  let path
  const context = {
    scale() {},
    beginPath: () => { path = [] },
    moveTo: (x, y) => path.push([x, y]),
    lineTo: (x, y) => path.push([x, y]),
    closePath() {},
    stroke: () => lines.push(path),
    fill: () => tips.push(path[0]),
  }
  const canvas = {
    ownerDocument: { defaultView: { devicePixelRatio: 2, getComputedStyle: () => ({ color: '#245948' }) } },
    getClientRects: () => [frame],
    getContext: () => context,
  }
  module.drawConnections(canvas, diagram, edges)
  assert.equal(canvas.width, 2000)
  assert.equal(canvas.height, 900)
  assert.equal(lines.length, edges.length)
  assert.equal(tips.length, edges.length)
  edges.forEach(({ source, target, key }, index) => {
    const from = cards.get(source.task.id)
    const to = cards.get(target.task.id)
    const label = rows.get(key)
    assert.deepEqual(lines[index][0], [from.right - frame.left, from.top - frame.top + 20])
    assert.deepEqual(tips[index], [to.left - frame.left - 2, label.top - frame.top + label.height / 2])
  })
  const detour = lines[edges.findIndex(edge => edge.key === '2-5')]
  assert.equal(detour[2][1], 10)
  assert.equal(detour[3][1], 10)
})

test('renders native task links and names every prerequisite independently of the canvas', () => {
  const dri = 'raft:agent:00000000-0000-0000-0000-000000000001'
  const namedItems = items.map(item => item.task === release
    ? { ...item, task: { ...item.task, dri, dri_profile: { name: 'Release Owner' } } }
    : item)
  const html = renderToStaticMarkup(React.createElement(module.MilestoneDiagram, { items: namedItems, taskHref }))
  const nodeLinks = [...html.matchAll(/<h3[^>]*><a href="#task-(\d+)"/g)].map(match => Number(match[1]))
  assert.deepEqual(nodeLinks, [1, 2, 3, 4, 5])
  const edges = [...html.matchAll(/data-milestone-edge="(\d+)-(\d+)"[\s\S]*?<a href="#task-(\d+)"/g)]
  assert.equal(edges.length, 5)
  for (const [, source, target, href] of edges) {
    assert.equal(href, source)
    assert.match(html, new RegExp(`aria-label="Prerequisites for T-${target}"`))
  }
  assert.match(html, /role="region" aria-label="Milestone dependency diagram"/)
  assert.match(html, /<canvas[^>]+aria-hidden="true"/)
  assert.match(html, /<span class="milestone-map-source-workstream">Core<\/span>/)
  assert.match(html, />In progress<\/span>/)
  assert.match(html, />Owner Alex<\/span>/)
  assert.match(html, />Owner Release Owner<\/span>/)
  assert.doesNotMatch(html, /raft:agent:/)
  const workstreams = renderToStaticMarkup(React.createElement(module.default, { taskHref }))
  assert.match(workstreams, /role="status">Loading workstreams/)
  assert.doesNotMatch(workstreams, /id="workstreams-title"/)
})

test('uses readable labels for unresolved and duplicate identities while keeping each filter key', () => {
  const agent = 'raft:agent:00000000-0000-0000-0000-000000000001'
  const reviewer = 'raft:human:00000000-0000-0000-0000-000000000002'
  const otherReviewer = 'raft:human:00000000-0000-0000-0000-000000000003'
  const filters = new Map()
  shared.addFilterName(filters, reviewer)
  shared.addFilterName(filters, otherReviewer, { name: otherReviewer })
  shared.addFilterName(filters, otherReviewer, { name: otherReviewer })
  assert.equal(shared.profileName(agent, { name: agent, username: 'worker' }), 'worker')
  assert.equal(shared.profileName(agent), 'Raft agent')
  assert.deepEqual([...filters], [['Raft human', reviewer], ['Raft human 2', otherReviewer]])
})

test('retains both recorded edges if two milestones depend on each other', () => {
  const { columns, edges } = module.buildMilestoneGraph([
    { task: parser, workstream: 'Core', prerequisites: [writer] },
    { task: writer, workstream: 'Output', prerequisites: [parser] },
  ])
  assert.deepEqual(columns.map(column => column.map(({ task }) => task.id)), [[3, 4]])
  assert.deepEqual(edges.map(({ key }) => key).sort(), ['3-4', '4-3'])
  assert.ok(edges.every(({ track }) => Number.isInteger(track)))
})
