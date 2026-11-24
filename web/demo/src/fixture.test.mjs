import assert from 'node:assert/strict'
import test from 'node:test'
import { previewRequest } from './fixture.js'

const get = path => previewRequest(path, 'GET')

test('the historical walkthrough and the development workflow use separate projects', () => {
  assert.deepEqual(get('/projects').items.map(project => project.prefix), ['T', 'FLOW'])
  assert.deepEqual(get('/tasks?project=T').items.map(task => task.key), ['T-1406'])
  assert.ok(get('/tasks?project=FLOW').items.every(task => task.key.startsWith('FLOW-')))
  assert.equal(get('/activity/2').event.task_key, 'T-1406')
})

test('workflow tasks cover the board states and link a blocker to its source', () => {
  const taskStates = get('/tasks?project=FLOW').items.map(task => task.display_state || task.state)
  assert.deepEqual([...new Set(taskStates)].sort(),
    ['closed', 'done', 'human_review', 'in_progress', 'parked', 'ready', 'waiting'])
  const waits = get('/tasks?project=FLOW&state=waiting').items
  assert.deepEqual([...new Set(waits.map(task => task.wait_kind))].sort(), ['automation', 'person', 'ticket'])
  const blocked = waits.find(task => task.wait_kind === 'ticket')
  const [blocker] = blocked.blockers.tickets
  const dependency = get('/tasks/' + blocker.key)
  assert.equal(dependency.task.state, 'in_progress')
  const [wait] = get('/tasks/' + blocked.key + '/activity').items
  assert.equal(wait.id, blocked.event_id)
  assert.equal(get('/tasks/' + blocked.key + '/history/' + wait.id).event.id, wait.id)
})

test('queued and acknowledged asks resolve to a task and its requesting ticket', () => {
  const requests = get('/reports/request-log?project=FLOW').items
  const queued = requests.find(request => request.phase === 'asked')
  const finished = requests.find(request => request.phase === 'done')
  assert.ok(queued && finished)
  const detail = get('/tasks/' + queued.request_key)
  assert.equal(detail.task.peer_request.source_key, queued.source_key)
  assert.equal(detail.task.state, 'ready')
  assert.equal(get('/tasks/' + queued.source_key).task.state, 'waiting')
  assert.ok(get('/activity/' + finished.response.id).event)
  assert.ok(get('/activity/' + finished.resolution.id).event)
})

test('the reviewer report shows the queued ask and the ask already in progress', () => {
  const reports = get('/reports?project=FLOW&view=active').items
  const reviewer = reports.find(report => report.profile.name === 'Blake')
  const asks = reviewer.tasks.filter(task => task.peer_request)
  assert.deepEqual(asks.map(task => task.peer_request.phase).sort(), ['asked', 'in_progress'])
  for (const task of asks) {
    assert.equal(task.peer_request.requester_name, 'Avery')
    const source = get('/tasks/' + task.peer_request.source_key)
    assert.equal(source.task.state, 'waiting')
    assert.equal(source.task.context.peer_ask_ticket, task.key)
  }
})

test('the fixture surfaces a decision and a staged Knowledge entry with original ticket links', () => {
  const [decision] = get('/notifications/needs-you?project=FLOW').items
  assert.equal(decision.kind, 'decision')
  assert.equal(get('/tasks/' + decision.ticket).task.state, 'human_review')
  const [area] = get('/knowledge/areas').areas
  const [entry] = get('/knowledge?entry=' + area.index).items
  const [stage] = entry.staged_work
  assert.equal(get('/tasks/' + stage.source).task.state, 'in_progress')
  assert.throws(() => previewRequest('/tasks/' + decision.ticket, 'POST'), { status: 403 })
})


test('all-project activity interleaves the fixtures by timestamp without changing anchors', () => {
  const { items } = get('/activity?scope=all&detail=all')
  assert.ok(items.some(item => item.task_key.startsWith('FLOW-')))
  assert.ok(items.some(item => item.task_key === 'T-1406'))
  const times = items.map(item => Date.parse(item.at))
  assert.deepEqual(times, [...times].sort((first, second) => second - first))
  assert.equal(get('/activity/1').event.task_key, 'T-1406')
})

test('outcome days and links use the selected time zone', () => {
  for (const [timeZone, day, otherDay] of [
    ['UTC', '2026-11-24', '2026-11-23'],
    ['America/Los_Angeles', '2026-11-23', '2026-11-24'],
  ]) {
    const filters = new URLSearchParams({ project: 'FLOW', time_zone: timeZone })
    assert.deepEqual(get('/outcomes/days?' + filters).days, [day])
    assert.ok(get('/outcomes?' + filters + '&day=' + day).items.length > 0)
    assert.deepEqual(get('/outcomes?' + filters + '&day=' + otherDay).items, [])
  }
})
