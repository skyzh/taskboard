import { createContext, useContext } from 'react'

const storageKey = 'tasks-board.time-zone'
export const browserTimeZone = new Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
const commonTimeZones = [
  'America/Los_Angeles', 'America/Chicago', 'America/New_York',
  'Europe/London', 'Europe/Paris', 'Asia/Kolkata', 'Asia/Shanghai', 'Asia/Tokyo',
  'Australia/Sydney', 'Pacific/Auckland',
]
const supported = Intl.supportedValuesOf?.('timeZone') || commonTimeZones
export const timeZones = [...new Set([...supported, browserTimeZone])].filter(zone => zone !== 'UTC').sort()
timeZones.unshift('UTC')

export function readTimeZonePreference() {
  try {
    const saved = window.localStorage.getItem(storageKey)
    if (timeZones.includes(saved)) return saved
  } catch {
    // Use the browser time zone when storage is unavailable.
  }
  return ''
}

export function saveTimeZonePreference(preference) {
  try {
    if (preference) window.localStorage.setItem(storageKey, preference)
    else window.localStorage.removeItem(storageKey)
  } catch {
    // The selection can still apply until the page is reloaded.
  }
}

export function createTimeZoneFormatters(timeZone) {
  const dayFormatter = new Intl.DateTimeFormat('en-CA', {
    year: 'numeric', month: '2-digit', day: '2-digit', timeZone,
  })
  const timeOptions = { hour: 'numeric', minute: '2-digit', timeZoneName: 'short', timeZone }
  const timeFormatter = new Intl.DateTimeFormat('en-US', timeOptions)
  const clockFormatter = new Intl.DateTimeFormat('en-US', { ...timeOptions, second: '2-digit' })
  const dateTimeFormatter = new Intl.DateTimeFormat('en-US', {
    ...timeOptions, month: 'short', day: 'numeric', year: 'numeric',
  })
  const shortTime = new Intl.DateTimeFormat(undefined, { timeStyle: 'short', timeZone })
  const shortDateTime = new Intl.DateTimeFormat(undefined, { dateStyle: 'short', timeStyle: 'short', timeZone })
  const localInputTime = new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', timeZone,
  })
  function formatDay(at) {
    const date = Object.fromEntries(dayFormatter.formatToParts(new Date(at))
      .filter(part => part.type !== 'literal').map(part => [part.type, part.value]))
    return `${date.year.padStart(4, '0')}-${date.month}-${date.day}`
  }
  return {
    timeZone,
    formatDay,
    formatLocalInput(at) {
      const time = localInputTime.format(new Date(at)).replace(/:00$/, '')
      return `${formatDay(at)}T${time}`
    },
    formatCompactTimestamp(at, now = Date.now()) {
      const formatter = formatDay(at) === formatDay(now) ? shortTime : shortDateTime
      return formatter.format(new Date(at))
    },
    formatTimestamp(at, showDate = true) {
      return (showDate ? dateTimeFormatter : timeFormatter).format(new Date(at))
    },
    formatClock(at) {
      return clockFormatter.format(new Date(at))
    },
  }
}

export const TimeZoneContext = createContext(createTimeZoneFormatters(browserTimeZone))

export function useTimeZone() {
  return useContext(TimeZoneContext)
}
