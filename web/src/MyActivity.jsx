import React from 'react'
import { useLoadedPages } from './data.js'
import { LoadMore, ResourceStatus, Timestamp, profileName } from './shared.jsx'
import { EventRow } from './Work.jsx'

const providers = {
  github: 'GitHub',
  'local-file': 'Local file',
}

function SourceContext({ event }) {
  const { context } = event
  const provider = context && providers[context.source_kind]
  const source = provider === profileName(event.actor, event.actor_profile) ? null : provider
  if (!source && !context?.provider_at) return null
  return (
    <p className="activity-source-meta">
      {source && <span>{source}</span>}
      {context.provider_at && <span>Changed <Timestamp at={context.provider_at} /></span>}
    </p>
  )
}

export default function MyActivity({ revision }) {
  const page = useLoadedPages('/my-activity', { scope: 'all' }, revision)
  const errors = page.data?.source_errors || []
  return (
    <section className="panel my-activity" aria-labelledby="my-activity">
      <h2 id="my-activity">My activity</h2>
      <ResourceStatus resource={page} label="your activity" />
      {errors.length > 0 && (
        <section className="activity-source-errors" aria-labelledby="activity-source-errors-title">
          <h3 id="activity-source-errors-title">Source updates unavailable</h3>
          <ul className="context-list">
            {errors.map(({ source_key: source, error, at }, index) => (
              <li key={index}>
                <strong>{source}</strong>
                {at && <> · <Timestamp at={at} /></>}
                <p>{error}</p>
              </li>
            ))}
          </ul>
        </section>
      )}
      {page.data?.items.length === 0 && <p className="section-message" role="status">No activity on tasks you own or watch.</p>}
      {page.data?.items.length > 0 && (
        <ol className="event-list" role="list">
          {page.data.items.map(event => (
            <EventRow key={event.id} event={event} showTask>
              <SourceContext event={event} />
            </EventRow>
          ))}
        </ol>
      )}
      <LoadMore page={page} label="activity entries" />
    </section>
  )
}
