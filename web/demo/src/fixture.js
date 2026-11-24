import { ownerHref, taskHref } from '../../src/shared.jsx'

export const repository = 'https://github.com/skyzh/taskboard'
export const setupGuide = repository + '/blob/main/docs/taskboard/SETUP.md#initial-agent-prompt'
export const personalAgentsGuide = repository + '/blob/main/docs/taskboard/PERSONAL-AGENTS.md'
export const project = { key: 'T', title: 'Inventory Export' }
export const capturedAt = '2026-09-16T18:00:00Z'

export const profiles = new Map([
  ['avery', { name: 'Avery', kind: 'human', role: 'Project lead' }],
  ['juniper', { name: 'Juniper', kind: 'agent', role: 'Export implementation' }],
])

function task(id, title, state, owner, note, options = {}) {
  return {
    id, key: `T-${id}`, title, state, note, priority: 'normal',
    dri: owner, dri_profile: profiles.get(owner), public_url: taskHref(id),
    summary_label: { in_progress: 'Next step', ready: 'Next step', human_review: 'Decision', done: 'Outcome' }[state],
    ...options,
  }
}

export const decision = task(1, 'Choose which fields to include in the inventory export', 'done', 'avery',
  'The first CSV export includes SKU, item name, and quantity on hand.', {
    brief: 'Give store managers a small CSV they can open in a spreadsheet to review inventory counts.',
    checklist_total: 2,
    checklist_completed: 2,
    checklist: [
      { position: 1, label: 'Compare sample CSV files with store managers', checked: true },
      { position: 2, label: 'Record the fields and follow-up work', checked: true },
    ],
    context: { choices: ['SKU, item name, and quantity on hand', 'Include supplier costs in the first export'] },
  })
export const review = task(2, 'Review the export with a store manager', 'in_progress', 'juniper',
  'Compare the sample files against the inventory counts and record the result for Avery.', {
    peer_request: { phase: 'in_progress', source_id: decision.id, source_key: decision.key },
    checklist_total: 2,
    checklist_completed: 1,
    checklist: [
      { position: 1, label: 'Create a sample with empty quantities and quoted names', checked: true },
      { position: 2, label: 'Review the sample with Avery', checked: false },
    ],
  })
export const weeklyRun = task(3, 'Check the weekly inventory export', 'ready', 'juniper',
  'Compare the latest sample with the approved field list.')
export const approval = task(4, 'Decide whether the export should include archived items', 'human_review', 'avery',
  'Choose whether managers should see archived items in the first export.', {
    context: { choices: ['Active items only', 'Include archived items'] },
  })
export const checkedNames = task(7, 'Check that item names survive CSV export', 'done', 'juniper',
  'The sample preserved commas and quotation marks in item names.', {
    peer_request: { phase: 'done', source_id: decision.id, source_key: decision.key },
  })
export const tasks = [decision, review, weeklyRun, approval, checkedNames]

function activity(id, record, actor, action, label, at, note, context) {
  return {
    id, task_id: record.id, task_key: record.key, task_title: record.title, public_url: record.public_url,
    actor, actor_profile: profiles.get(actor), action, label, at, note, context,
  }
}

export const responseEvent = activity(10, checkedNames, 'juniper', 'done', 'Response',
  '2026-09-15T12:30:00Z', checkedNames.note)
export const decisionEvent = activity(11, decision, 'avery', 'decide', 'Decision recorded',
  '2026-09-16T14:10:00Z', 'Store managers will receive one CSV for each store.', { choice: 'SKU, item name, and quantity on hand' })
export const askEvent = activity(12, review, 'avery', 'log', 'Request',
  '2026-09-16T15:00:00Z', 'Asked Juniper to check a sample export with a store manager.')
export const reviewEvent = activity(13, review, 'juniper', 'update', 'Progress',
  '2026-09-16T16:30:00Z', 'The sample contains the approved columns and keeps quoted item names intact.')
export const scheduleEvent = activity(14, weeklyRun, 'juniper', 'add', 'Created',
  '2026-09-16T17:30:00Z', 'The weekly export check is ready.')
export const approvalEvent = activity(15, approval, 'avery', 'assign-human', 'Decision requested',
  '2026-09-16T17:45:00Z', approval.note)
export const events = [approvalEvent, scheduleEvent, reviewEvent, askEvent, decisionEvent, responseEvent]

export const knowledge = {
  area: 'Inventory Export',
  index: { key: 'T-5', title: 'Inventory export decisions', brief: 'Record the decisions that affect the CSV store managers receive.' },
  note: { key: 'T-6', title: 'Inventory CSV fields', brief: decision.note },
}
export const knowledgeHref = key => '#knowledge?' + new URLSearchParams({ entry: key })

export const reports = [...profiles].map(([owner, profile]) => {
  const current = tasks.filter(task => task.dri === owner && task.state !== 'done')
  return {
    owner, profile, public_url: ownerHref(owner, undefined, project.key),
    last_activity: events.find(event => event.actor === owner),
    tasks: current,
    task_count: current.length,
    goals: owner === 'avery'
      ? [{ kind: 'standing', summary: 'Keep the export easy to review in a spreadsheet.', public_url: knowledgeHref(knowledge.note.key) }]
      : [{ kind: 'standing', summary: 'Keep a weekly sample aligned with the approved fields.', ticket: weeklyRun.key, public_url: weeklyRun.public_url }],
    recent_outcomes: owner === 'avery' ? [decisionEvent] : [responseEvent],
  }
})

function requestRow(task, record, phase, response) {
  return {
    id: task.id, phase, at: record.at, request_key: task.key, request_title: task.title,
    requester: 'avery', requester_profile: profiles.get('avery'),
    recipient: 'juniper', recipient_profile: profiles.get('juniper'),
    next_owner: response ? null : 'juniper', next_owner_profile: profiles.get('juniper'),
    source_id: decision.id, source_key: decision.key, source_title: decision.title,
    ...(response && { response }),
  }
}

export const requestLog = [
  requestRow(review, askEvent, 'in_progress'),
  requestRow(checkedNames, responseEvent, 'done', responseEvent),
]
