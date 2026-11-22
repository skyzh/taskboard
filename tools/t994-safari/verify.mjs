import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const root = process.argv[2]
const read = relative => JSON.parse(readFileSync(join(root, relative)))
for (const layout of ['compact', 'wide']) {
  const before = read(`before/${layout}.json`)
  const after = read(`after/${layout}.json`)
  const compact = layout === 'compact'
  for (const data of [before, after]) {
    assert.ok(!data.error, data.error)
    assert.match(data.ua, /iPhone.*Safari/)
    assert.equal(data.viewport.width, compact ? 402 : 768)
    const {dpr, scale} = data.viewport
    if (compact) assert.equal(dpr, 3)
    else assert.ok(dpr === 3 || Math.abs(dpr / scale - 3) < .05, `Unexpected device scale ${dpr} at ${scale}`)
    assert.equal(data.overflow, false)
    assert.match(data.waiting, /Waiting/)
    assert.deepEqual(data.tasks.map(row => row.key), ['T-1', 'T-2'])
    assert.ok(data.tasks.every(row => row.visuallyHidden === compact && row.titleBaseline.unchanged))
    assert.ok(data.tasks[1].titleLines > 1)
  }
  assert.deepEqual(after.tasks.map(row => [row.dot, row.titleLines]), before.tasks.map(row => [row.dot, row.titleLines]))
  if (compact) {
    assert.deepEqual(after.tasks.map(row => row.compactDotCenter), before.tasks.map(row => row.compactDotCenter))
  } else {
    assert.ok(before.tasks.every(row => row.statusBaseline.unchanged && Math.abs(row.gap) >= .25))
    assert.ok(after.tasks.every(row => row.statusBaseline.unchanged && Math.abs(row.gap) < .02))
  }
}
