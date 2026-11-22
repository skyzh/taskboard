import React from 'react'
import { Timestamp } from './shared.jsx'

export default function ActiveRelease({ receipt }) {
  if (!receipt) return null
  const unverified = receipt.status === 'rollback_failed'
  return (
    <aside className="active-release" aria-label="Active release">
      <h4>{unverified ? 'Active release unverified' : 'Active release'}</h4>
      {!unverified && <p>
        {receipt.release_number != null && <>Release #{receipt.release_number} · </>}
        <code>{receipt.active}</code>
      </p>}
      <p className="event-meta">
        {unverified && 'Rollback failed · '}
        {receipt.status === 'rolled_back' && 'Restored by rollback · '}
        <Timestamp at={receipt.at} />
        {' · '}
        {receipt.receipt_url
          ? <a href={receipt.receipt_url}>Receipt <code>{receipt.release_id}</code></a>
          : <>Receipt <code>{receipt.release_id}</code></>}
      </p>
    </aside>
  )
}
