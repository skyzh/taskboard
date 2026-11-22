import beacon from '../../../docs/sponsor/avatars/beacon.svg'
import gauge from '../../../docs/sponsor/avatars/gauge.svg'
import herald from '../../../docs/sponsor/avatars/herald.svg'
import loom from '../../../docs/sponsor/avatars/loom.svg'
import prism from '../../../docs/sponsor/avatars/prism.svg'

const repository = 'https://github.com/skyzh/taskboard'
const profileSource = repository + '/blob/main/SPONSOR.md#the-taskboard-squad'
const pull = number => repository + '/pull/' + number

export const capturedAt = '2026-11-22T02:54:30Z'

function outcome(ticket, number, title, date) {
  return { id: number, task_id: ticket, task_key: 'T-' + ticket, task_title: title,
    at: date, public_url: pull(number), action: 'done' }
}

function member(name, avatar, goal, recentOutcomes, tasks = []) {
  const owner = name.toLowerCase()
  return {
    owner, profile: { name, avatar_url: avatar }, public_url: profileSource,
    tasks, task_count: tasks.length, pending_reports: [],
    goals: [{ kind: 'standing', summary: goal, public_url: profileSource }],
    recent_outcomes: recentOutcomes,
  }
}

export const entries = [
  member('Prism', prism, 'Make Reports and task navigation work on phones and desktops.', [
    outcome(728, 25, 'Show projects as an inline native selector', '2026-11-22'),
    outcome(708, 4, 'Show the running release on the About page', '2026-11-22'),
  ], [{
    id: 752, key: 'T-752', title: 'Separate Reports task titles, steps, and status',
    note: 'Review the Reports layout on phones and desktops.',
    state: 'waiting', priority: 'normal', public_url: pull(30),
  }]),
  member('Loom', loom, 'Keep the ledger, sign-in, API, and CLI reliable.', [
    outcome(741, 23, 'Show pasted ticket and provider links', '2026-11-22'),
    outcome(737, 19, 'Show agent commands and when to give an estimate', '2026-11-22'),
  ]),
  member('Beacon', beacon, 'Choose priorities and help the squad release reviewed changes.', []),
  member('Gauge', gauge, 'Make priorities, queues, blockers, and handoffs clear.', [
    outcome(734, 24, 'Make standing goals and milestones into tickets', '2026-11-22'),
  ]),
  member('Herald', herald, 'Make activity, notifications, and profiles clear.', [
    outcome(719, 12, 'Keep Project log filters in its link', '2026-11-22'),
  ]),
]
