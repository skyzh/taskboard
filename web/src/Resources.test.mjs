import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

let vite
let ResourcesContents
let ResourceHistoryRows
let matchesResourceReadback
let resourceRequest

before(async () => {
  vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, ws: false, hmr: false, watch: null },
  })
  ;({ ResourcesContents, ResourceHistoryRows, matchesResourceReadback, resourceRequest } =
    await vite.ssrLoadModule('/src/Resources.jsx'))
})

after(() => vite?.close())

const path = '/projects/T/resources'
const claim = {
  claim_id: 'claim-1', holder: { id: 'private-principal', name: 'Avery' }, ticket: 'T-42', url: null,
  claimed_at: '2030-04-01T16:00:00Z', expires_at: '2030-04-01T16:30:00Z', revision: 1,
}
const resource = {
  key: 'staging-slot', title: 'Staging slot', conditions: '<verify staging>\nHold the slot while tests run.',
  max_minutes: 30, can_claim: false, can_renew: false, can_release: false, claim,
}

function render(items, canDefine) {
  return renderToStaticMarkup(React.createElement(ResourcesContents, {
    data: { project: 'T', can_define: canDefine, items }, writeContext: { project: 'T', listPath: path }, revision: 1,
  }))
}

test('shows the project claim, plain-text conditions, and no unauthorized writes', () => {
  const html = render([resource], false)
  assert.match(html, /Staging slot/)
  assert.match(html, /&lt;verify staging&gt;\nHold the slot while tests run\./)
  assert.match(html, /<dd>Avery<\/dd>/)
  assert.match(html, /href="#task-T-42"/)
  assert.match(html, /dateTime="2030-04-01T16:00:00Z"/)
  assert.match(html, /dateTime="2030-04-01T16:30:00Z"/)
  assert.match(html, /<summary>Claim history<\/summary>/)
  assert.doesNotMatch(html, /Define resource|Edit definition|Claim resource|Renew claim|Release claim|private-principal/)
})

test('renders manager definition and only the available claim actions', () => {
  const available = { ...resource, claim: null, can_claim: true }
  const claimHtml = render([available], true)
  assert.match(claimHtml, /Edit definition/)
  assert.match(claimHtml, /Define resource/)
  assert.match(claimHtml, /<input type="number" min="1" max="30" required="" name="minutes"/)
  assert.match(claimHtml, /<input type="checkbox" required="" name="checked_conditions"/)
  assert.match(claimHtml, /I checked the claim conditions\./)
  assert.doesNotMatch(claimHtml, /Renew claim|Release claim/)
  const holderHtml = render([{ ...resource, can_renew: true, can_release: true }], false)
  assert.match(holderHtml, /Renew claim/)
  assert.match(holderHtml, /Release claim/)
  assert.doesNotMatch(holderHtml, /Claim resource|Define resource/)
})

test('renders historical claim phases, actorless expiry, and safe ticket and PR links', () => {
  const rows = [
    { event_id: 4, action: 'expire', at: claim.expires_at, actor: null, claim },
    { event_id: 3, action: 'release', at: claim.claimed_at, actor: claim.holder,
      claim: { ...claim, ticket: null, url: 'https://github.com/fictional/repo/pull/3' } },
    { event_id: 2, action: 'renew', at: claim.claimed_at, actor: claim.holder,
      claim: { ...claim, ticket: null, url: 'javascript:unsafe()' } },
    { event_id: 1, action: 'claim', at: claim.claimed_at, actor: claim.holder, claim },
  ]
  const html = renderToStaticMarkup(React.createElement(ResourceHistoryRows, { items: rows }))
  for (const label of ['Expired', 'Released', 'Renewed', 'Claimed']) assert.match(html, new RegExp(`<strong>${label}<\/strong>`))
  assert.match(html, /href="#task-T-42"/)
  assert.match(html, /href="https:\/\/github\.com\/fictional\/repo\/pull\/3"/)
  assert.doesNotMatch(html, /javascript:|private-principal/)
})

test('an ambiguous submission reuses its key only for the exact operation and payload', () => {
  let counter = 0
  const next = () => `key-${++counter}`
  const body = { claim_id: claim.claim_id, revision: claim.revision, minutes: 20 }
  const renew = `${path}/staging-slot/renew`
  const first = resourceRequest(null, renew, body, next)
  assert.equal(resourceRequest(first, renew, { ...body }, next), first)
  assert.notEqual(resourceRequest(first, renew, { ...body, revision: 2 }, next).key, first.key)
  assert.notEqual(resourceRequest(first, `${path}/staging-slot/release`, body, next).key, first.key)
})

test('checks each saved result against the signed project list', () => {
  const receipt = { project: 'T', key: resource.key, claim }
  const data = { project: 'T', items: [resource] }
  for (const action of ['claim', 'renew']) {
    assert.equal(matchesResourceReadback(data, 'T', resource.key, action, {}, receipt), true)
    assert.equal(matchesResourceReadback(data, 'T', resource.key, action, {},
      { ...receipt, claim: { ...claim, revision: 2 } }), false)
  }
  assert.equal(matchesResourceReadback(data, 'LAB', resource.key, 'claim', {}, receipt), false)
  assert.equal(matchesResourceReadback(data, 'T', resource.key, 'release', {}, receipt), false)
  assert.equal(matchesResourceReadback({ ...data, items: [{ ...resource, claim: null }] },
    'T', resource.key, 'release', {}, { ...receipt, claim: null }), true)
  assert.equal(matchesResourceReadback(data, 'T', resource.key, 'define', resource, {}), true)
  assert.equal(matchesResourceReadback(data, 'T', resource.key, 'define', { ...resource, max_minutes: 60 }, {}), false)
})
