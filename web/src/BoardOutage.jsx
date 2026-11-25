import React, { createContext } from 'react'

export const BoardOutageContext = createContext(false)

export function ReadPlaceholder({ label, compact = false }) {
  return (
    <div className={compact ? 'read-placeholder compact' : 'read-placeholder'}>
      <span className="visually-hidden">{label} could not be loaded.</span>
      <span className="read-placeholder-line" aria-hidden="true" />
    </div>
  )
}

export default function BoardOutage({ checking, onRetry }) {
  return (
    <aside className="board-outage" aria-labelledby="board-outage-title">
      <div role="alert">
        <h2 id="board-outage-title">Taskboard is temporarily unavailable</h2>
        <p>A deployment may be in progress. If this continues, contact your Taskboard service owner.</p>
      </div>
      <button type="button" aria-disabled={checking} onClick={checking ? undefined : onRetry}>
        {checking ? 'Checking…' : 'Retry loading'}
      </button>
    </aside>
  )
}
