import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { effectiveTheme } from './theme.js'
import { browserTimeZone } from './timeZone.js'

let vite
let Profile
let Settings
let CodexUsageDetails
let CodexUsageSources

before(async () => {
  vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, hmr: false, ws: false, watch: null },
  })
  Profile = (await vite.ssrLoadModule('/src/Profile.jsx')).default
  Settings = (await vite.ssrLoadModule('/src/Settings.jsx')).default
  const usage = await vite.ssrLoadModule('/src/CodexUsage.jsx')
  CodexUsageDetails = usage.CodexUsageDetails
  CodexUsageSources = usage.CodexUsageSources
})
after(() => vite?.close())

const theme = { mode: 'day', preference: '' }
const agent = { data: { actor: { id: 'Avery', name: 'Avery', kind: 'agent' } } }
const portal = { data: { actor: { name: 'Portal reader', kind: 'portal' }, sign_in_available: true } }

test('private quota presentation separates paid and reset credits, including unavailable values', () => {
  const usage = {
    sample: true, captured_at: '2026-11-01T12:00:00Z',
    buckets: [{ label: 'Codex', primary: { remaining_percent: 73.5, duration_minutes: 300, resets_at: null },
      secondary: { remaining_percent: 0, duration_minutes: 10080, resets_at: 1793548800 } }],
    paid_balance: '25.50', paid_unlimited: false, reset_credits: 0,
  }
  const html = renderToStaticMarkup(React.createElement(CodexUsageDetails, { usage }))
  assert.match(html, /Sample data/)
  assert.match(html, /5-hour window.*73.5% remaining.*Reset time unavailable/)
  assert.match(html, /7-day window.*>0% remaining.*Resets/)
  assert.match(html, /<dt>Paid credits<\/dt><dd>25.50<\/dd>/)
  assert.match(html, /<dt>Rate-limit reset credits<\/dt><dd>0<\/dd>/)
  const unlimited = renderToStaticMarkup(React.createElement(CodexUsageDetails, {
    usage: { ...usage, paid_unlimited: true, reset_credits: null },
  }))
  assert.match(unlimited, /<dt>Paid credits<\/dt><dd>Unlimited<\/dd>/)
  assert.match(unlimited, /<dt>Rate-limit reset credits<\/dt><dd>Unavailable<\/dd>/)
})

test('private quota sources show independent capture and availability', () => {
  const html = renderToStaticMarkup(React.createElement(CodexUsageSources, { data: { sources: [
    { label: 'Source A', usage: { sample: true, captured_at: '2026-11-01T12:00:00Z', buckets: [], paid_balance: '25.50' } },
    { label: 'Source B', usage: null },
  ] } }))
  assert.match(html, /<h3>Source A<\/h3>.*Sample data.*Last read <time datetime="2026-11-01T12:00:00Z">/i)
  assert.match(html, /Paid credits.*25.50.*<h3>Source B<\/h3><p>Codex usage is unavailable.<\/p>$/)
})

test('pre-paint scheme and React agree on explicit, system and unavailable storage', () => {
  const script = readFileSync(new URL('../public/assets/theme.js', import.meta.url), 'utf8')
  for (const [stored, systemDark, expected] of [
    ['day', true, 'light'], ['night', false, 'dark'],
    [null, true, 'dark'], ['invalid', false, 'light'], [new Error('Storage disabled'), true, 'dark'],
  ]) {
    const root = { dataset: {} }
    const meta = {}
    runInNewContext(script, {
      localStorage: {
        getItem(key) {
          assert.equal(key, 'tasks-board.theme')
          if (stored instanceof Error) throw stored
          return stored
        },
      },
      document: { documentElement: root, querySelector: () => meta },
      matchMedia: query => {
        assert.equal(query, '(prefers-color-scheme: dark)')
        return { matches: systemDark }
      },
    })
    const preference = stored === 'day' || stored === 'night' ? stored : undefined
    assert.equal(root.dataset.theme, preference)
    assert.equal(root.dataset.colorScheme, expected)
    assert.equal(meta.content, expected)
    assert.equal(effectiveTheme(preference, systemDark), expected === 'dark' ? 'night' : 'day')
  }
})

test('profile actions use a native disclosure button while editing lives in Settings', () => {
  const header = renderToStaticMarkup(React.createElement(Profile, { session: agent, fragment: '#reports', theme }))
  const panel = header.match(/aria-expanded="false" aria-controls="([^"]+)" aria-label="Avery’s profile"/)?.[1]
  assert.ok(panel)
  assert.match(header, /<button type="button" class="profile-toggle"/)
  assert.ok(header.includes(`<div id="${panel}" class="profile-actions" hidden="">`))
  assert.match(header, /role="group" aria-label="Color theme"/)
  assert.match(header, /aria-label="Auto" title="Auto" aria-pressed="true"/)
  assert.match(header, /aria-label="Day" title="Day" aria-pressed="false"/)
  assert.match(header, /aria-label="Night" title="Night" aria-pressed="false"/)
  assert.match(header, /href="#settings">Settings/)
  assert.match(header, /<button type="button">Sign out<\/button>/)
  assert.doesNotMatch(header, /<form|Save profile/)

  const settings = renderToStaticMarkup(React.createElement(Settings, { session: agent, theme }))
  assert.match(settings, /<h1 id="settings">Settings/i)
  assert.match(settings, /Color theme/)
  assert.match(settings, /Display time zone/)
  assert.match(settings, /value="" selected="">Browser time zone<\/option>/)
  const zoneId = settings.match(/aria-describedby="([^"]+)"/)?.[1]
  assert.ok(zoneId)
  assert.ok(settings.includes(`class="settings-browser-zone" id="${zoneId}">${browserTimeZone}</p>`))
  assert.match(settings, /Role<input/)
  assert.match(settings, /Description<textarea/)
  assert.match(settings, /Avatar<input/)
  assert.match(settings, />Save profile<\/button>/)
})

test('Settings shows labeled native theme radios with the saved option checked', () => {
  for (const preference of ['', 'day', 'night']) {
    const settings = renderToStaticMarkup(React.createElement(Settings, { session: portal, theme: { ...theme, preference } }))
    const group = settings.match(/<fieldset[^>]*><legend>Color theme<\/legend>(.*?)<\/fieldset>/)?.[1]
    assert.ok(group)
    const radios = [...group.matchAll(/<label><input type="radio" name="color-theme"( checked="")? value="([^"]*)"\/>(Day|Night|Auto)<\/label>/g)]
    assert.deepEqual(radios.map(([, , value, label]) => [value, label]), [['day', 'Day'], ['night', 'Night'], ['', 'Auto']])
    assert.deepEqual(radios.filter(([, checked]) => checked).map(([, , value]) => value), [preference])
    assert.doesNotMatch(group, /<select|role=|tabindex=/i)
  }
})

test('only signed-in agents and humans have a Sign out action', () => {
  const human = { data: { actor: { id: 'River', name: 'River', kind: 'human' } } }
  const local = { data: { actor: { name: 'Local reader', kind: 'local' } } }
  for (const session of [agent, human]) {
    const header = renderToStaticMarkup(React.createElement(Profile, { session, fragment: '#reports', theme }))
    assert.match(header, />Sign out<\/button>/)
    assert.doesNotMatch(header, />Sign in with Raft<\/a>/)
  }
  for (const session of [portal, local, { error: { status: 401 } }]) {
    const header = renderToStaticMarkup(React.createElement(Profile, { session, fragment: '#reports', theme }))
    assert.doesNotMatch(header, />Sign out<\/button>/)
  }
})

test('portal and signed-out viewers can change appearance and still sign in', () => {
  const night = { ...theme, mode: 'night', preference: 'night' }
  for (const session of [portal, { error: { status: 401 } }]) {
    const header = renderToStaticMarkup(React.createElement(Profile, { session, fragment: '#task-1', theme: night }))
    assert.match(header, /aria-label="Not signed in"/)
    assert.match(header, />Not signed in<\/span>/)
    assert.match(header, /aria-label="Night" title="Night" aria-pressed="true"/)
    assert.match(header, /href="#settings">Settings/)
    assert.match(header, /href="\.\/auth\/login\?return_to=%2Ftickets%2F1">Sign in with Raft<\/a>/)
    const settings = renderToStaticMarkup(React.createElement(Settings, { session, theme: night, timeZonePreference: 'UTC' }))
    assert.match(settings, /Color theme/)
    assert.match(settings, /value="UTC" selected="">UTC<\/option>/)
    assert.doesNotMatch(settings, /settings-browser-zone|aria-describedby/)
    assert.match(settings, /href="\.\/auth\/login\?return_to=%2Fsettings"/)
    assert.doesNotMatch(settings, /<form|Save profile/)
  }
})
