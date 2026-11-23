import React, { useState } from 'react'
import { useLoadedPages } from './data.js'
import { Markdown, MarkdownInline } from './Markdown.jsx'
import { LoadMore, ResourceStatus, eventHref, signInHref, taskHref } from './shared.jsx'

function entryHref(key) {
  return '#knowledge?' + new URLSearchParams({ entry: key })
}

function KnowledgeSearch({ search, onSearch }) {
  const [draft, setDraft] = useState(search)
  return (
    <form className="knowledge-search" role="search" onSubmit={event => {
      event.preventDefault()
      onSearch(draft.trim())
    }}>
      <label>
        Search decisions
        <input type="search" value={draft} maxLength={160} onChange={event => setDraft(event.target.value)} />
      </label>
      <button type="submit">Search</button>
    </form>
  )
}

function DecisionSources({ sources }) {
  if (!sources.length) return null
  return (
    <ul className="knowledge-sources" aria-label="Decision sources" role="list">
      {sources.map(source => (
        <li key={source.event_id} data-decision-at={source.at}>
          <a href={taskHref(source.task_key)}>{source.task_key}</a>
          <a href={eventHref(source.event_id)}>{source.label}</a>
        </li>
      ))}
    </ul>
  )
}

export default function Knowledge({ session, verified, revision, search, entry, onSearch }) {
  const page = useLoadedPages(verified ? '/knowledge' : null, { q: entry ? undefined : search, entry }, revision)
  const signIn = session.data?.sign_in_available || session.error?.status === 401
  const fragment = entry ? entryHref(entry) : search ? '#knowledge?' + new URLSearchParams({ q: search }) : '#knowledge'
  return (
    <section className="knowledge" aria-labelledby="knowledge">
      <div className="page-heading"><h1 id="knowledge" tabIndex={-1}>Knowledge</h1>
        {entry && <a href="#knowledge">All decisions</a>}
      </div>
      {verified ? <>
        <KnowledgeSearch key={search} search={search} onSearch={onSearch} />
        <ResourceStatus resource={page} label="decisions" />
        {page.data?.items.length === 0 && <p className="section-message">
          {search || entry ? 'No matching decisions.' : 'No maintained decisions yet.'}
        </p>}
        {page.data?.items.length > 0 && (
          <ol className="knowledge-list" aria-label="Maintained decisions" role="list">
            {page.data.items.map(item => (
              <li className="panel" key={item.id}>
                <h2><a href={entryHref(item.key)}><MarkdownInline text={item.title} links={false} /></a></h2>
                <a className="ticket-id" href={taskHref(item.key)}>{item.key}</a>
                {item.summary && <Markdown text={item.summary} />}
                <DecisionSources sources={item.sources} />
              </li>
            ))}
          </ol>
        )}
        <LoadMore page={page} label="decisions" />
      </> : signIn ? <a href={signInHref(fragment)}>Sign in to view decisions</a>
        : <ResourceStatus resource={session} label="your session" />}
    </section>
  )
}

export function TicketKnowledgeLinks({ ticket, revision }) {
  const page = useLoadedPages('/knowledge', { source: ticket }, revision)
  if (!page.data?.items.length && !page.error) return null
  return (
    <section className="ticket-knowledge" aria-label="Knowledge entries">
      <h2>Knowledge</h2>
      <ResourceStatus resource={page} label="linked decisions" />
      <ul className="context-list" role="list">
        {page.data?.items.map(item => (
          <li key={item.id}><a href={entryHref(item.key)}><MarkdownInline text={item.title} links={false} /></a></li>
        ))}
      </ul>
      <LoadMore page={page} label="linked decisions" />
    </section>
  )
}
