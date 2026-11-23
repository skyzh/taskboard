import React, { useState } from 'react'
import { request } from './data.js'
import { useTimeZone } from './timeZone.js'

export function ReminderValue({ timed, value, onChange, timeZone, disabled }) {
  return timed ? (
    <label>Defer until ({timeZone})<input type="datetime-local" value={value} step="1" required
      disabled={disabled} onChange={event => onChange(event.target.value)} /></label>
  ) : (
    <label>Date (Pacific time)<input type="date" value={value} required
      disabled={disabled} onChange={event => onChange(event.target.value)} /></label>
  )
}

export default function ReminderDate({ path, saved, savedUntil, exactTime = false, onChanged }) {
  const { timeZone, formatLocalInput } = useTimeZone()
  const savedMode = savedUntil ? 'time' : 'date'
  const savedTime = savedUntil ? formatLocalInput(savedUntil) : ''
  const [mode, setMode] = useState(savedMode)
  const [date, setDate] = useState(saved || '')
  const [time, setTime] = useState(savedTime)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const timed = mode === 'time'
  const changed = mode !== savedMode || (timed ? time !== savedTime : date !== (saved || ''))
  async function save(body) {
    setPending(true)
    setError('')
    try {
      await request(path, { method: 'PUT', body })
      if (body.date === null) {
        setDate('')
        setTime('')
        setMode('date')
      }
      onChanged()
    } catch (failure) {
      setError(failure.message)
    } finally {
      setPending(false)
    }
  }
  return (
    <details>
      <summary>{saved || savedUntil ? 'Edit reminder' : 'Set a reminder'}</summary>
      <form onSubmit={event => { event.preventDefault(); save(timed ? { at: time, time_zone: timeZone } : { date }) }}
        data-reload-blocked={pending || changed}>
        {exactTime && <label>Reminder type<select value={mode} onChange={event => setMode(event.target.value)}>
          <option value="date">Date only</option>
          <option value="time">Exact time</option>
        </select></label>}
        <ReminderValue timed={timed} value={timed ? time : date}
          onChange={timed ? setTime : setDate} timeZone={timeZone} />
        <button type="submit" disabled={pending || !(timed ? time : date) || !changed}>Save reminder</button>
        {(saved || savedUntil) && <button type="button" disabled={pending}
          onClick={() => save({ date: null })}>Clear reminder</button>}
        {error && <p role="alert">{error}</p>}
      </form>
    </details>
  )
}
