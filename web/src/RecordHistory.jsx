import React from 'react'
import { useLoadedPages, useResource } from './data.js'
import { Markdown, MarkdownInline } from './Markdown.jsx'
import { ReportTime, ReportTimeProvider } from './ReportTime.jsx'
import { LoadMore, ResourceStatus, Status, Timestamp, eventHref, profileName } from './shared.jsx'

export function historyHref(currentHref, event = 'all') {
  const [anchor, query] = currentHref.split('?', 2)
  const params = new URLSearchParams(query)
  params.set('history', event)
  return `${anchor}?${params}`
}

function VersionTimeline({ timeline, currentHref, selected }) {
  const events = timeline.data?.items ?? []
  const selectionOutsidePage = selected && timeline.data?.next_cursor != null && !events.some(event => event.id === selected.id)
  return (
    <nav className="version-timeline" aria-label="Version timeline">
      <h2>Version timeline</h2>
      <ResourceStatus resource={timeline} label="version history" />
      {timeline.data && <>
        {events.length === 0 && <p className="section-message">No recorded activity yet.</p>}
        {selectionOutsidePage && <p className="event-meta">Selected version · <ReportTime at={selected.at} linked /></p>}
        <ol>
          {events.map(event => (
            <li key={event.id}>
              <a href={historyHref(currentHref, event.id)}
                aria-current={selected?.id === event.id ? 'location' : undefined}>
                <span>{event.label}</span>
                <small><ReportTime at={event.at} linked /> · {profileName(event.actor, event.actor_profile)}</small>
              </a>
            </li>
          ))}
        </ol>
        <LoadMore page={timeline} label="recorded versions" />
      </>}
    </nav>
  )
}

function AdjacentVersions({ previous, next, currentHref }) {
  if (!previous && !next) return null
  return (
    <nav className="version-neighbors" aria-label="Adjacent versions">
      {previous && <a href={historyHref(currentHref, previous.id)}>
        <span>Previous version</span><small><ReportTime at={previous.at} linked /></small>
      </a>}
      {next && <a href={historyHref(currentHref, next.id)}>
        <span>Next version</span><small><ReportTime at={next.at} linked /></small>
      </a>}
    </nav>
  )
}

export default function RecordHistory({ record, currentHref, history, revision, knowledge }) {
  const base = `/tasks/${encodeURIComponent(record)}`
  const timeline = useLoadedPages(`${base}/activity`, {}, revision)
  const version = useResource(history === 'all' ? null : `${base}/history/${encodeURIComponent(history)}`, revision)
  const past = version.data
  const versionTimeline = <VersionTimeline timeline={timeline} currentHref={currentHref} selected={past?.event} />
  return (
    <ReportTimeProvider>
      <section className="record-history" aria-labelledby="record-history">
        <div className="page-heading">
          <h1 id="record-history">History for {record}</h1>
          <a href={currentHref}>Back to current version</a>
        </div>
        {history === 'all' ? versionTimeline : (
          <div className="record-history-layout">
            <div>
              <a href={historyHref(currentHref)}>All versions</a>
              <ResourceStatus resource={version} label="this version" />
              {past && <>
                <AdjacentVersions previous={past.previous} next={past.next} currentHref={currentHref} />
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
            </div>
            {versionTimeline}
          </div>
        )}
      </section>
    </ReportTimeProvider>
  )
}
