import assert from 'node:assert/strict'
import test from 'node:test'
import {
  browserTimeZone, createTimeZoneFormatters, readTimeZonePreference, saveTimeZonePreference, timeZones,
} from './timeZone.js'

test('the same UTC instant uses the selected calendar day and clock time', () => {
  const at = '2026-11-21T07:59:59Z'
  const cases = [
    ['America/Los_Angeles', '2026-11-20', /Nov 20, 2026.*11:59\s*PM PST/],
    ['UTC', '2026-11-21', /Nov 21, 2026.*7:59\s*AM UTC/],
    ['Pacific/Honolulu', '2026-11-20', /Nov 20, 2026.*9:59\s*PM HST/],
    ['Asia/Kathmandu', '2026-11-21', /Nov 21, 2026.*1:44\s*PM GMT\+5:45/],
  ]
  for (const [zone, day, timestamp] of cases) {
    const { formatDay, formatTimestamp } = createTimeZoneFormatters(zone)
    assert.equal(formatDay(at), day, zone)
    assert.match(formatTimestamp(at), timestamp, zone)
    assert.doesNotMatch(formatTimestamp(at, false), /Nov/, zone)
  }
})

test('compact timestamps show the date only across the selected zone’s local day boundary', () => {
  const timeZone = 'America/Los_Angeles'
  const at = '2026-11-21T07:59:00Z'
  const { formatCompactTimestamp } = createTimeZoneFormatters(timeZone)
  const clock = new Intl.DateTimeFormat(undefined, { timeStyle: 'short', timeZone })
  const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: 'short', timeStyle: 'short', timeZone })
  assert.equal(formatCompactTimestamp(at, '2026-11-21T07:59:59Z'), clock.format(new Date(at)))
  assert.equal(formatCompactTimestamp(at, '2026-11-21T08:00:00Z'), dateTime.format(new Date(at)))
})

test('timestamps distinguish both occurrences of a clock time during daylight saving rollback', () => {
  const { formatDay, formatTimestamp, formatClock } = createTimeZoneFormatters('America/Los_Angeles')
  assert.equal(formatDay('2026-11-01T08:30:00Z'), '2026-11-01')
  assert.match(formatTimestamp('2026-11-01T08:30:00Z', false), /1:30\s*AM PDT/)
  assert.equal(formatDay('2026-11-01T09:30:00Z'), '2026-11-01')
  assert.match(formatTimestamp('2026-11-01T09:30:00Z', false), /1:30\s*AM PST/)
  assert.match(formatClock('2026-11-01T09:30:04Z'), /1:30:04\s*AM PST/)
})

test('the browser time zone is the default and a saved override can be restored and cleared', () => {
  assert.equal(browserTimeZone, new Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC')
  assert.ok(timeZones.includes(browserTimeZone))
  assert.ok(timeZones.includes('UTC'))
  const entries = new Map()
  globalThis.window = {
    localStorage: {
      getItem: key => entries.get(key),
      setItem: (key, value) => entries.set(key, value),
      removeItem: key => entries.delete(key),
    },
  }
  try {
    assert.equal(readTimeZonePreference(), '')
    saveTimeZonePreference('Asia/Tokyo')
    assert.equal(readTimeZonePreference(), 'Asia/Tokyo')
    saveTimeZonePreference('')
    assert.equal(readTimeZonePreference(), '')
    saveTimeZonePreference('Invalid/Zone')
    assert.equal(readTimeZonePreference(), '')
    delete globalThis.window.localStorage
    assert.equal(readTimeZonePreference(), '')
  } finally {
    delete globalThis.window
  }
})
