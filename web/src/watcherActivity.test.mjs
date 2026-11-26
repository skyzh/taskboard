import assert from 'node:assert/strict'
import { test } from 'node:test'
import { groupWatcherActivity } from './watcherActivity.js'

const source = 'https://github.com/fictional/board/pull/17'

function check(id, fields = {}, result = {}) {
  return {
    id, task_id: 1, actor: 'GitHub extension', action: 'log', state: 'in_progress',
    note: 'Portable checks passed.',
    links: [
      { label: 'GitHub check', url: `https://github.com/fictional/board/actions/runs/${id}` },
      { label: 'GitHub pull request', url: source },
    ],
    context: {
      extension: 'github-events', github_event: 'ci-job', github_source: source,
      job: 'Portable checks', conclusion: 'success', head_sha: 'a'.repeat(40),
      check_run_id: id, github_activity_id: `github:ci-job:${id}`, provider_at: String(id), ...result,
    },
    ...fields,
  }
}

test('groups consecutive check reruns from the same source and revision without losing any event', () => {
  const latest = check(5)
  const earlier = check(4)
  const checkpoint = check(3, { context: { checkpoint: true } })
  const previousPage = [latest, earlier]
  assert.deepEqual(groupWatcherActivity(previousPage), [[latest, earlier]])
  const next = check(2)
  const oldest = check(1)
  const all = [...previousPage, checkpoint, next, oldest]
  assert.deepEqual(groupWatcherActivity(all), [[latest, earlier], [checkpoint], [next, oldest]])
  assert.deepEqual(groupWatcherActivity(all).flat(), all)
  assert.deepEqual(previousPage, [latest, earlier])
})

test('keeps transitions, decisions, review results, and different watcher results separate', () => {
  const first = check(100)
  for (const different of [
    check(99, {}, { github_source: 'https://github.com/fictional/board/pull/18' }),
    check(99, {}, { head_sha: 'b'.repeat(40) }),
    check(99, {}, { conclusion: 'failure' }),
    check(99, { note: 'Portable checks failed.' }),
    check(99, { details: 'An additional result.' }),
    check(99, { links: [...first.links, { label: 'Other evidence', url: 'https://example.test/report' }] }),
    check(99, { action: 'done' }),
    check(99, { action: 'decide' }),
    check(99, {}, { github_event: 'review-approved' }),
    check(99, { state: 'ready' }),
  ]) {
    assert.deepEqual(groupWatcherActivity([first, different]), [[first], [different]])
  }
})

test('legacy entries need an identified source and exactly matching content and evidence', () => {
  const first = check(10, { action: 'source-event', actor: 'Jira', context: {} })
  const duplicate = { ...first, id: 9 }
  const otherLink = { ...first, id: 8, links: [{ label: 'Source', url: 'https://example.test/other' }] }
  const ordinaryLog = { ...first, action: 'log', actor: 'reader' }
  const unknownSource = { ...first, links: [] }
  assert.deepEqual(groupWatcherActivity([first, duplicate, otherLink]), [[first, duplicate], [otherLink]])
  assert.deepEqual(groupWatcherActivity([ordinaryLog, ordinaryLog]), [[ordinaryLog], [ordinaryLog]])
  assert.deepEqual(groupWatcherActivity([unknownSource, unknownSource]), [[unknownSource], [unknownSource]])
})
