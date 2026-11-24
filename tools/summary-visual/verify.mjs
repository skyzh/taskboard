import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const [evidence, ...layouts] = process.argv.slice(2)
const widths = { compact: 402, wide: 768, desktop: 1280 }
const aligned = (first, second) => Math.abs(first - second) <= .1
for (const layout of layouts) {
  const data = JSON.parse(await readFile(join(evidence, `${layout}.json`), 'utf8'))
  assert.ok(!data.error, data.error)
  assert.match(data.ua, layout === 'desktop' ? /Chrom(e|ium)/ : /iPhone.*Safari/)
  assert.equal(data.viewport.width, widths[layout])
  assert.equal(data.overflow, false)
  assert.deepEqual(data.tasks.map(({ key, state }) => [key, state]), [
    ['T-1', 'In progress'], ['T-2', 'In progress'], ['T-3', 'Waiting'],
  ])
  for (const row of data.tasks) {
    const { heading, status, step } = row
    assert.ok(row.visibleStatus, `${layout}: ${row.key} status must be visible`)
    if (layout === 'compact') {
      assert.ok(status.bottom <= heading.top && heading.bottom <= step.top, `${row.key}: expected status, ticket, then step`)
      assert.ok(aligned(status.left, heading.left) && aligned(heading.left, step.left))
    } else {
      assert.ok(heading.right <= status.left, `${row.key}: ticket must be in the left column`)
      assert.ok(aligned(heading.top, status.top))
      assert.ok(aligned(status.left, step.left) && status.bottom <= step.top, `${row.key}: expected status above step on the right`)
    }
  }
  const waiting = data.tasks[2]
  assert.match(waiting.dependency?.text, /Waiting on Blake/)
  assert.ok(waiting.step.bottom <= waiting.dependency.bounds.top, 'Waiting context must follow the current step')
  assert.ok(data.tasks[1].titleLines > 1, 'The long sample must wrap')
  console.log(JSON.stringify({ layout, width: data.viewport.width, verifiedRows: data.tasks.length, longTitleLines: data.tasks[1].titleLines }))
}
