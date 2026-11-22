import React, { useState } from 'react'
import { useLoadedPages } from './data.js'
import { eventHref, LoadMore, ResourceStatus, TaskLink, Timestamp } from './shared.jsx'

const phases = {
  asked: 'Asked',
  in_progress: 'In progress',
  acknowledgment: 'Ready for acknowledgment',
  done: 'Acknowledged',
  canceled: 'Canceled',
}

function RecordedEvent({ label, event }) {
  return (
    <a href={eventHref(event.id)}>
      {label} <Timestamp at={event.at} />
    </a>
  )
}

export default function PeerRequestHistory({ project, revision }) {
  const [open, setOpen] = useState(false)
  const page = useLoadedPages('/reports/peer-request-history', { project }, revision, { limit: 10, enabled: open })
  return (
    <details className="peer-request-history" onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>Request history</summary>
      {open && (
        <>
          <p className="report-note">History shows linked peer requests. Older named waits may have no request record.</p>
          <ResourceStatus resource={page} label="request history" />
          {!page.error && page.data?.items.length === 0 && <p role="status">No linked peer requests recorded.</p>}
          {page.data?.items.length > 0 && (
            <ul className="report-outcomes peer-history-list" aria-label="Recorded peer requests" role="list">
              {page.data.items.map(ask => (
                <li key={ask.id}>
                  <div className="peer-history-line">
                    <strong>{ask.requester} asked {ask.recipient}</strong>
                    <span className="ask-state">{phases[ask.phase]}</span>
                  </div>
                  <div className="peer-history-line">
                    <TaskLink id={ask.id} reference={ask.task_key} title="Request" />
                    <TaskLink id={ask.source_id} reference={ask.source_key} title={ask.source_title} />
                    {ask.asked_at && <span>Asked <Timestamp at={ask.asked_at} /></span>}
                  </div>
                  {(ask.response || ask.resolution) && (
                    <div className="peer-history-line">
                      {ask.response && <RecordedEvent label="Response" event={ask.response} />}
                      {ask.resolution && <RecordedEvent label={phases[ask.phase]} event={ask.resolution} />}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
          <LoadMore page={page} label="request history" />
        </>
      )}
    </details>
  )
}
