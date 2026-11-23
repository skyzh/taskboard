import React from 'react'
import { useLoadedPages, useResource } from './data.js'
import { Markdown, MarkdownInline } from './Markdown.jsx'
import { LoadMore, ResourceStatus, Status, Timestamp, eventHref, profileName } from './shared.jsx'

export function historyHref(currentHref, event = 'all') {
  const [anchor, query] = currentHref.split('?', 2)
  const params = new URLSearchParams(query)
  params.set('history', event)
  return `${anchor}?${params}`
}

export default function RecordHistory({ record, currentHref, history, revision, knowledge }) {
  const base = `/tasks/${encodeURIComponent(record)}`
  const timeline = useLoadedPages(history === 'all' ? `${base}/activity` : null, {}, revision)
  const version = useResource(history === 'all' ? null : `${base}/history/${encodeURIComponent(history)}`, revision)
  const past = version.data
  return (
    <section className="record-history" aria-labelledby="record-history">
      <div className="page-heading">
        <h1 id="record-history">History for {record}</h1>
        <a href={currentHref}>Back to current version</a>
      </div>
      {history === 'all' ? (
        <>
          <ResourceStatus resource={timeline} label="version history" />
          {timeline.data && <>
            {timeline.data.items.length === 0 && <p className="section-message">No recorded activity yet.</p>}
            <ul className="context-list" aria-label="Recorded versions">
              {timeline.data.items.map(event => (
                <li key={event.id}>
                  <a href={historyHref(currentHref, event.id)}>
                    {event.label} · <Timestamp at={event.at} />
                  </a>
                  {' · '}{profileName(event.actor, event.actor_profile)}
                </li>
              ))}
            </ul>
            <LoadMore page={timeline} label="recorded versions" />
          </>}
        </>
      ) : (
        <>
          <a href={historyHref(currentHref)}>All versions</a>
          <ResourceStatus resource={version} label="this version" />
          {past && <>
            <p className="event-meta">
              Read-only version · <Timestamp at={past.event.at} /> · {profileName(past.event.actor, past.event.actor_profile)}
              {' · '}<a href={eventHref(past.event.id)}>{past.event.label}</a>
            </p>
            <h2>{past.title === null ? 'Title unavailable' : <MarkdownInline text={past.title} links={false} />}</h2>
            {!knowledge && past.state && <p><Status state={past.state} /></p>}
            {past.brief === null ? <p className="section-message">The description was not recorded for this version.</p>
              : <Markdown text={past.brief} />}
            {!knowledge && past.note && <section>
              <h3>Work summary</h3>
              <Markdown text={past.note} />
            </section>}
          </>}
        </>
      )}
    </section>
  )
}
