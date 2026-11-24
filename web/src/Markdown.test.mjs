import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { evidenceLinkLabel } from './sourceLinks.js'

const render = (Component, props) => renderToStaticMarkup(React.createElement(Component, props))
let vite
let markdown
let shared

before(async () => {
  vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, ws: false, hmr: false, watch: null },
  })
  markdown = await vite.ssrLoadModule('/src/Markdown.jsx')
  shared = await vite.ssrLoadModule('/src/shared.jsx')
})
after(() => vite?.close())

test('renders block paragraphs, ordered and nested lists, and code as HTML structure', () => {
  const text = [
    'First **paragraph**.', '', 'Second *paragraph*.',
    '', '3. First item', '   - Nested item', '4. Next item',
    '', '~~~', '<code>argument</code>', '~~~',
  ].join('\n')
  const body = render(markdown.Markdown, { text })
  assert.match(body, /<p>First <strong>paragraph<\/strong>\.<\/p><p>Second <em>paragraph<\/em>\.<\/p>/)
  assert.match(body, /<ol start="3"><li>First item<ul><li>Nested item<\/li><\/ul><\/li><li>Next item<\/li><\/ol>/)
  assert.match(body, /<pre><code>&lt;code&gt;argument&lt;\/code&gt;\n<\/code><\/pre>/)
})

test('labels default GitHub links and retains authored link text, Raft URLs and safe navigation', () => {
  const body = render(markdown.Markdown, { text: [
    'https://github.com/example/tool/pull/14',
    '<https://github.com/example/tool/issues/9>',
    '[Compatibility notes](https://github.com/example/tool/pull/15/files)',
    '[Work chat](https://example.raft.build/channel/abc)',
    '[Recorded outcome](#event-12-log)',
  ].join('\n\n') })
  assert.match(body, /href="https:\/\/github\.com\/example\/tool\/pull\/14">Pull request example\/tool #14<\/a>/)
  assert.match(body, /href="https:\/\/github\.com\/example\/tool\/issues\/9">Issue example\/tool #9<\/a>/)
  assert.match(body, /href="https:\/\/github\.com\/example\/tool\/pull\/15\/files">Compatibility notes<\/a>/)
  assert.match(body, /href="https:\/\/example\.raft\.build\/channel\/abc">Work chat<\/a>/)
  assert.match(body, /href="#event-12-log">Recorded outcome<\/a>/)
})

test('escapes raw HTML and prevents unsupported destinations, image loads and nested anchors', () => {
  const text = [
    '<img src=x onerror=alert(1)>', '[unsafe](javascript:alert%281%29)',
    '[local](file:///private/source.md)', '[foreign](//example.test/resource)',
    '![Avatar](https://example.test/avatar.png)',
    '![Raft screenshot](https://app.raft.build/artifacts/example)',
    '[![Linked image](https://example.test/avatar.png)](https://example.test/other)',
  ].join('\n\n')
  const body = render(markdown.Markdown, { text })
  assert.match(body, /&lt;img src=x onerror=alert\(1\)&gt;/)
  assert.match(body, /<a href="https:\/\/example\.test\/avatar\.png">Avatar<\/a>/)
  assert.match(body, /<a href="https:\/\/app\.raft\.build\/artifacts\/example">Raft screenshot<\/a>/)
  assert.match(body, /<a href="https:\/\/example\.test\/other">Linked image<\/a>/)
  assert.doesNotMatch(body, /<img\b|<script\b|href="(?:javascript:|file:|\/\/)/)
  assert.equal([...body.matchAll(/<a\b/g)].length, 3)
  const title = render(shared.TaskLink, {
    id: 11, title: '**Build** [spec](https://example.test/spec) and \u0060draft\u0060', current: true,
  })
  assert.match(title, /href="#task-11" aria-current="true"/)
  assert.match(title, /<strong>Build<\/strong> spec and <code>draft<\/code>/)
  assert.equal([...title.matchAll(/<a\b/g)].length, 1)
})

test('shows local evidence preview controls without loading any images', () => {
  const links = [
    { label: 'Screenshot', url: 'file:///fixture/device.png', preview_url: './api/v1/local-evidence/1/0' },
    { label: 'Notes', url: 'file:///fixture/notes.txt' },
    { label: 'Hidden', unavailable: true, preview_url: './api/v1/local-evidence/1/2' },
    { label: 'External', url: 'https://example.test/image.png', preview_url: './api/v1/local-evidence/1/3' },
    { label: 'Invalid preview', url: 'file:///fixture/other.png', preview_url: 'https://example.test/other.png' },
  ]
  const result = render(shared.EvidenceLinks, { links })
  assert.match(result, /aria-expanded="false" aria-label="Preview image for Screenshot"/)
  assert.match(result, /<a href="https:\/\/example\.test\/image\.png">External<\/a>/)
  assert.match(result, /Show local path for Screenshot/)
  assert.doesNotMatch(result, /<img\b|local-evidence\/|https:\/\/example\.test\/other\.png/)
  assert.equal([...result.matchAll(/<button\b/g)].length, 1)
})

test('shows GitHub state beside its source link and preserves unannotated links', () => {
  const links = [
    { label: 'Pull request', url: 'https://github.com/example/tool/pull/14', github: { state: 'merged' } },
    { label: 'Issue', url: 'https://github.com/example/tool/issues/22', github: { state: 'open', stale: true } },
    { label: 'Unverified', url: 'https://github.com/example/tool/pull/15', github: { state: null, stale: true } },
    { label: 'Manual', url: 'https://example.test/notes' },
  ]
  const result = render(shared.EvidenceLinks, { links, label: 'Sources' })
  for (const [index, link] of links.slice(0, 3).entries()) {
    const description = result.match(new RegExp(`aria-describedby="([^"]+-github-${index})"`))?.[1]
    assert.ok(description)
    const reference = ['example/tool#14', 'example/tool#22', 'example/tool#15'][index]
    assert.ok(result.includes(`<a href="${link.url}" aria-describedby="${description}">${link.label} · ${reference}</a>`))
    assert.ok(result.includes(`<span id="${description}" class="github-source-status">${['Merged', 'Last seen open', 'Status unavailable'][index]}</span>`))
  }
  assert.ok(result.includes('<li><a href="https://example.test/notes">Manual</a></li>'))
})

test('adds compact recognized references to source and evidence links', () => {
  const links = [
    { label: 'Compatibility notes', url: 'https://github.com/example/tool/pull/14/files?diff=split#discussion_r42' },
    { label: 'Already named (Example/Tool #14)', url: 'https://github.com/example/tool/pull/14' },
    { label: 'Follow-up to example/tool#140', url: 'https://github.com/example/tool/pull/14' },
    { label: 'Decision record', url: 'https://stamp.build/s/team/github/example/tool/pull/14/approvals/deadbeef/evidence' },
    { label: 'Fix revision', url: 'https://github.com/example/tool/commit/cafebabe1234567' },
    { label: 'Original task', url: '#task-COURSE-19' },
    { label: 'Checkup', url: '#event-23-log' },
    { label: 'Manual', url: 'https://example.test/example/tool/pull/14' },
  ]
  const result = render(shared.EvidenceLinks, { links, label: 'Sources', ticketSources: true })
  const labels = [
    'Compatibility notes · example/tool#14', 'Already named (Example/Tool #14)',
    'Follow-up to example/tool#140 · example/tool#14', 'Decision record · example/tool#14',
    'Fix revision · example/tool@cafebab', 'Original task · COURSE-19', 'Checkup · Activity #23', 'Manual',
  ]
  for (const [index, link] of links.entries()) {
    assert.ok(result.includes(`<a href="${link.url}">${labels[index]}</a>`))
  }
  const board = new URL('https://board.example/tasks/')
  assert.equal(evidenceLinkLabel(`${board}#task-7`, 'Source', { board }), 'Source · T-7')
  assert.equal(evidenceLinkLabel('https://other.example/tasks/#task-7', 'Source', { board }), 'Source')
  assert.equal(evidenceLinkLabel('https://board.example/another/#task-7', 'Source', { board }), 'Source')
})

test('renders Markdown in historical details and checklist labels without changing evidence gates', () => {
  const links = [
    { label: 'GitHub · example/tool#14', url: 'https://github.com/example/tool/pull/14', generated: true },
    { label: 'GitHub', url: 'https://github.com/example/tool/pull/22' },
    { label: 'Tracking', url: 'https://github.com/example/tool/issues/9' },
    { label: 'Private evidence', unavailable: true, url: 'file:///private/withheld' },
  ]
  const event = render(shared.OutcomeNote, {
    event: { note: 'Legacy **result**.\n\n- [Read notes](https://example.test/notes)', links },
  })
  assert.match(event, /Legacy <strong>result<\/strong>/)
  assert.match(event, /<ul><li><a href="https:\/\/example\.test\/notes">Read notes<\/a><\/li><\/ul>/)
  assert.match(event, /Pull request · example\/tool#14/)
  assert.match(event, /href="https:\/\/github\.com\/example\/tool\/pull\/22">GitHub · example\/tool#22<\/a>/)
  assert.match(event, /href="https:\/\/github\.com\/example\/tool\/issues\/9">Tracking · example\/tool#9<\/a>/)
  assert.match(event, /Private evidence <span class="evidence-unavailable">\(local file\)<\/span>/)
  assert.doesNotMatch(event, /private\/withheld/)
  const steps = render(shared.ChecklistSteps, {
    steps: [{ position: 1, label: 'Check \u0060--dry-run\u0060', checked: false }],
  })
  assert.match(steps, /aria-label="Pending"><svg[^>]*aria-hidden="true"[^>]*>.*<\/svg><\/span><span class="checklist-step-number">1<\/span><span class="checklist-step-copy">Check <code>--dry-run<\/code>/)
})

test('expanded checklist names the starter and completer without inventing legacy attribution', () => {
  const started = '2026-11-21T01:00:00Z'
  const completed = '2026-11-21T02:00:00Z'
  const checklist = render(shared.ChecklistSteps, { steps: [
    {
      position: 1, label: 'Read', checked: 1, started_at: started, completed_at: completed,
      started_by: 'Avery', started_by_name: 'Avery Reader',
      completed_by: 'Blake', completed_by_name: 'Blake Writer',
    },
    { position: 2, label: 'Archived check', checked: 1, completed_at: completed },
  ] })
  assert.match(checklist, /Started <time[^>]*>[^<]+<\/time> by Avery Reader/)
  assert.match(checklist, /Completed <time[^>]*>[^<]+<\/time> by Blake Writer/)
  assert.match(checklist, /Archived check<small[^>]*><span[^>]*> · <\/span>Completed <time[^>]*>[^<]+<\/time><\/small>/)
})

test('links known ticket text without altering existing links or code', () => {
  const relatedTasks = [
    { key: 'T-2', url: 'https://board.example/tasks/#task-2' },
    { key: 'COURSE-19', url: 'https://board.example/tasks/#task-COURSE-19' },
  ]
  const result = render(markdown.Markdown, { relatedTasks, text: [
    'See t-002, **COURSE-19** and unknown T-90.',
    '[T-2](https://example.test/existing) and \u0060T-2\u0060.',
    'Pasted https://board.example/tasks/#task-COURSE-19 and [open course ticket](#task-COURSE-19).',
    '~~~\nCOURSE-19\n~~~',
  ].join('\n\n') })
  assert.match(result, /href="https:\/\/board\.example\/tasks\/#task-2">t-002<\/a>/)
  assert.match(result, /<strong><a href="https:\/\/board\.example\/tasks\/#task-COURSE-19">COURSE-19<\/a><\/strong>/)
  assert.match(result, /<a href="https:\/\/example\.test\/existing">T-2<\/a> and <code>T-2<\/code>/)
  assert.match(result, /<pre><code>COURSE-19\n<\/code><\/pre>/)
  assert.match(result, /unknown T-90/)
  assert.match(result, /href="https:\/\/board\.example\/tasks\/#task-COURSE-19">COURSE-19<\/a>/)
  assert.match(result, /href="#task-COURSE-19">open course ticket<\/a>/)
  assert.equal([...result.matchAll(/<a\b/g)].length, 5)

  const checklist = render(shared.ChecklistSteps, {
    relatedTasks, steps: [{ position: 1, label: 'Review T-2', checked: false }],
  })
  assert.match(checklist, /Review <a href="https:\/\/board\.example\/tasks\/#task-2">T-2<\/a>/)
  const details = render(shared.OutcomeNote, {
    headline: 'Completed', event: { note: 'Completed', details: 'See T-2', related_tasks: relatedTasks },
  })
  assert.match(details, /See <a href="https:\/\/board\.example\/tasks\/#task-2">T-2<\/a>/)
})
