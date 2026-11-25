import assert from 'node:assert/strict'
import { test } from 'node:test'
import { legacyLocation, pageFragment, routeAddress } from './boardRoutes.js'

test('published page and record links retain their meaning at canonical paths', () => {
  for (const [fragment, path] of [
    ['#board', '/tasks'],
    ['#section=queue-heading&project=LAB', '/tasks?project=LAB#queue-heading'],
    ['#reports-all?view=watching', '/reports?view=watching&scope=all'],
    ['#dri=agent%3AAvery&section=outcomes-title&project=T', '/reports/agents/agent%3AAvery?project=T#outcomes-title'],
    ['#task-20', '/tickets/20'], ['#task-LAB-2?history=4', '/tickets/LAB-2?history=4'],
    ['#knowledge?entry=LAB-8&history=all', '/knowledge/LAB-8?history=all'],
    ['#schedule-R-1?project=LAB', '/schedules/R-1?project=LAB'],
    ['#event-26-log?actor=Avery', '/activity/events/26?actor=Avery'],
    ['#notifications-needs-me', '/notifications?view=needs-me'],
    ['#notifications-log?scope=all&kind=ready', '/notifications?scope=all&kind=ready&view=log'],
    ['#search=water+%26+land', '/search?q=water+%26+land'],
    ['#map?project=LAB', '/team-overview?project=LAB#team-overview'],
    ['#manage?project=LAB', '/settings?project=LAB&section=permissions'],
  ]) assert.equal(routeAddress(legacyLocation(fragment)), path, fragment)
  assert.deepEqual(legacyLocation('#section=queue-heading&project=LAB'),
    legacyLocation('#board?project=LAB&section=queue-heading'))
  assert.equal(legacyLocation('#checklist-title'), null)
})

test('canonical URLs supply the existing page filters and local anchors', () => {
  for (const [route, search, hash, params, fragment] of [
    ['home', '', '', {}, ''],
    ['reports', '?scope=all&view=watching', '', {}, 'reports-all?view=watching'],
    ['reports', '?view=goal-activity&scope=all', '', {}, 'reports?view=goal-activity&scope=all'],
    ['board', '?project=LAB', '#queue-heading', {}, 'board?project=LAB&section=queue-heading'],
    ['assignee', '?project=LAB', '#outcomes-title', { owner: 'agent:Avery' }, 'project=LAB&dri=agent%3AAvery&section=outcomes-title'],
    ['task', '?history=all', '', { id: 'LAB-1' }, 'task-LAB-1?history=all'],
    ['entry', '?history=2', '', { id: 'LAB-2' }, 'knowledge?history=2&entry=LAB-2'],
    ['schedule', '?project=LAB', '', { id: 'R-7' }, 'schedule-R-7?project=LAB'],
    ['event', '?action=done', '', { id: '6' }, 'event-6-log?action=done'],
    ['notifications', '?view=log&scope=all', '', {}, 'notifications-log?scope=all'],
    ['search', '?q=water+%26+land', '', {}, 'search=water+%26+land'],
  ]) assert.equal(pageFragment(route, { search, hash }, params), fragment, route)
})
