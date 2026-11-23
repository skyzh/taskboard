import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const [evidence, ...layouts] = process.argv.slice(2)
const widths = { compact: 402, wide: 768, desktop: 1280 }
const close = (a, b) => Math.abs(a - b) <= .1
for (const layout of layouts) {
  const data = JSON.parse(await readFile(join(evidence, `${layout}.json`), 'utf8'))
  assert.ok(!data.error, data.error)
  const compact = layout === 'compact'
  assert.match(data.ua, layout === 'desktop' ? /Chrom(e|ium)/ : /iPhone.*Safari/)
  assert.equal(data.viewport.width, widths[layout])
  assert.equal(data.overflow, false)
  assert.match(data.waiting, /Waiting\s*on Blake/)
  assert.equal(data.waitingBaselines.length, 3)
  assert.ok(data.waitingBaselines.every(item => item.unchanged))
  const [waiting, on, assignee] = data.waitingBaselines.map(item => item.value)
  assert.ok(close(waiting, on) && close(on, assignee), `${layout}: Waiting to on Blake gap is ${waiting - on} CSS px`)
  assert.deepEqual(data.tasks.map(row => row.key), ['T-1', 'T-2'])
  for (const row of data.tasks) {
    assert.equal(row.label, 'In progress')
    assert.equal(row.visuallyHidden, compact)
    assert.ok(row.titleBaseline.unchanged)
    assert.ok(close(row.titleIndentDifference, 0), `${layout}: wrapped title ${row.key} changed indentation`)
    if (compact) {
      assert.equal(row.dot.width, '8px')
      assert.equal(row.dot.height, '8px')
      assert.ok(row.compactDotCenter < 0 && row.compactDotCenter >= -12, 'The compact marker must sit on the first title line')
    } else {
      assert.ok(row.statusBaseline.unchanged)
      assert.ok(close(row.gap, 0), `${layout}: In progress to ${row.key} gap is ${row.gap} CSS px`)
    }
  }
  assert.ok(data.tasks[1].titleLines > 1, 'The long sample must wrap')
  console.log(JSON.stringify({ layout, width: data.viewport.width, waitingGap: waiting - on,
    inProgressGaps: data.tasks.map(row => row.gap), longTitleLines: data.tasks[1].titleLines }))
}
