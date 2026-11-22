import React, { useState } from 'react'
import { useLoadedPages } from './data.js'
import { Markdown } from './Markdown.jsx'
import { LoadMore, ResourceStatus, TaskLink, eventHref, ownerHref, profileName } from './shared.jsx'

const mergeStates = {
  reviewed: 'Ready to merge',
  conflict: 'Merge conflict',
  checks_failed: 'Checks failed',
}

function MergeEntries({ project, revision }) {
  const page = useLoadedPages('/merge-queue', { project }, revision)
  const entries = page.data?.items
  return (
    <>
      <ResourceStatus resource={page} label="the merge queue" />
      {entries?.length === 0 && <p className="section-message">No reviewed changes awaiting merge.</p>}
      {entries?.length > 0 && (
        <ol className="event-list" aria-label="Merge queue">
          {entries.map(entry => (
            <li className="event-row" key={entry.id}>
              <p>
                <TaskLink id={entry.task.id} reference={entry.task.key} title={entry.task.title} />
                {' · '}<strong>{mergeStates[entry.status]}</strong>
              </p>
              <p className="event-meta">
                <a href={ownerHref(entry.owner, undefined, project)}>{profileName(entry.owner, entry.owner_profile)}</a>
                {' · Target '}<code>{entry.target}</code>
                {' · '}<a href={eventHref(entry.review_event_id)}>Review receipt</a>
              </p>
              <p className="merge-candidate">Candidate <code>{entry.candidate}</code></p>
              {entry.status !== 'reviewed' && entry.note && <Markdown text={entry.note} />}
            </li>
          ))}
        </ol>
      )}
      <LoadMore page={page} label="changes in the merge queue" />
    </>
  )
}

export function MergeQueue({ project, revision }) {
  const [open, setOpen] = useState(false)
  return (
    <details onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>Merge queue</summary>
      {open && <MergeEntries project={project} revision={revision} />}
    </details>
  )
}
