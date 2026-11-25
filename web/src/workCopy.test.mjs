import assert from 'node:assert/strict'
import test from 'node:test'
import { activityExtraText, activityHeadline, activityResult, reportTaskCue, ticketActivityCopy, withoutRepeatedTitle } from './workCopy.js'

test('Reports leads with the explicit next step and keeps the wait condition', () => {
  const title = 'Verify the import checklist'
  const note = `${title}. Two checks passed. Next: Check duplicate rows.`
  assert.deepEqual(reportTaskCue({ note, title, state: 'in_progress' }), {
    text: 'Check duplicate rows.', label: 'Next step', details: 'Two checks passed.',
  })
  assert.deepEqual(reportTaskCue({ note: 'Send proof.\nWait for the signed receipt.', title, state: 'waiting' }), {
    text: 'Send proof.\nWait for the signed receipt.', label: undefined,
  })
  assert.equal(withoutRepeatedTitle(title, title), '')
  assert.equal(withoutRepeatedTitle('Inspect imports.', 'Inspect'), 'Inspect imports.')
})

test('Reports can render a published task with no work note', () => {
  assert.deepEqual(reportTaskCue({ title: 'Review the import', state: 'waiting' }), {
    text: '', label: undefined,
  })
})

test('Activity omits worker envelopes and keeps concrete findings', () => {
  const task_title = 'Review sample'
  assert.equal(activityResult({
    task_title,
    note: 'W-1 · Review worker Perry · Reported completed · Review sample. Two checks passed · one is pending.',
    context: { worker_report_id: 1 },
  }), 'Two checks passed · one is pending.')
  assert.equal(activityResult({
    task_title,
    note: 'Review sample. I completed the work. The header now matches the schema.',
  }), 'The header now matches the schema.')
  assert.equal(activityResult({
    task_title,
    note: 'I completed the work and found a duplicate header.',
  }), 'I completed the work and found a duplicate header.')
})

test('An authored headline takes precedence over handoff details', () => {
  assert.equal(activityHeadline({
    headline: 'The export now accepts the revised CSV.',
    note: 'Original note.',
    details: 'Build and fixture receipts.',
  }), 'The export now accepts the revised CSV.')
})

test('Older Activity keeps readable results, named waits and explicit holds', () => {
  const base = { task_title: 'Review the reader' }
  assert.equal(activityHeadline({
    ...base, action: 'log', note: 'Found an empty header. Check /tmp/fixtures/sample.csv',
  }), 'Found an empty header.')
  assert.equal(activityHeadline({
    ...base, action: 'wait', waiting_on: 'Avery', note: 'Review the candidate at /tmp/taskboard-review/candidate',
  }), 'Wait note Avery')
  assert.equal(activityHeadline({
    ...base, action: 'wait', wait_kind: 'person', waiting_profile: { name: 'Blake' }, note: 'Send the revised CSV.',
  }), 'Waiting on Blake · Send the revised CSV.')
  assert.equal(activityHeadline({
    ...base, action: 'wait', waiting_on: 'Build checks', wait_kind: 'automation',
    headline: 'Waiting on Build checks · Publish after green.',
  }), 'Blocked by Build checks · Publish after green.')
  assert.equal(activityHeadline({
    ...base, action: 'log', note: ['Checks passed. Do not deploy', 'a'.repeat(64), 'until approval.'].join(' '),
  }), 'Hold recorded')
  assert.equal(activityHeadline({
    ...base, action: 'log', note: 'Checks passed. Do not deploy until approval.',
  }), 'Do not deploy until approval.')
})


test('Long and multiline decisions keep their full Markdown in both Activity views', () => {
  for (const note of ['x'.repeat(181), 'Use the **sample** format.\n\n- Keep the headers.\n- Include examples.']) {
    const details = 'Check the preview before exporting.'
    const event = {
      action: 'decide', label: 'Decision recorded', context: { choice: 'Other' }, headline: note, note, details,
    }
    const headline = activityHeadline(event)
    assert.equal(headline, 'Decision recorded')
    assert.equal(activityExtraText(event, headline), note + '\n\n' + details)
    assert.deepEqual(ticketActivityCopy(event, headline), {
      choice: 'Other', result: '', details: note + '\n\n' + details,
    })
  }
})
