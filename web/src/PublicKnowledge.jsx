import React, { useEffect, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { queryPath, useLoadedPages, useResource } from './data.js'
import { Markdown } from './Markdown.jsx'
import { LoadMore, ResourceStatus, Timestamp } from './shared.jsx'

const indexPath = '/public/knowledge'
const publicId = /^[A-Za-z0-9_-]{1,80}$/

function SearchPublished({ query, onSearch }) {
  const [draft, setDraft] = useState(query)
  return <form className="knowledge-browse-controls" role="search" onSubmit={event => {
    event.preventDefault()
    onSearch(draft.trim())
  }}>
    <label>Search published Knowledge <input type="search" value={draft} maxLength={160}
      onChange={event => setDraft(event.target.value)} /></label>
    <button>Search</button>
  </form>
}

function PublishedIndex() {
  const [params, setParams] = useSearchParams()
  const query = params.get('q') || ''
  const page = useLoadedPages(indexPath, { q: query }, 0, { limit: 20 })
  return <>
    <h1>Published Knowledge</h1>
    <SearchPublished key={query} query={query} onSearch={value => setParams(value ? { q: value } : {})} />
    <ResourceStatus resource={page} label="published Knowledge" />
    {page.data?.items.length === 0 && <p className="section-message">{query
      ? 'No published entries match your search.' : 'No entries have been published.'}</p>}
    {page.data?.items.length > 0 && <ul className="knowledge-list" aria-label="Published entries" role="list">
      {page.data.items.filter(entry => publicId.test(entry.id)).map(entry => (
        <li className="panel" key={entry.id}>
          <h2><Link to={queryPath(`${indexPath}/${entry.id}`, { q: query })}>{entry.title}</Link></h2>
          {entry.excerpt && <p>{entry.excerpt}</p>}
          {entry.published_at && <p className="knowledge-last-activity">Published <Timestamp at={entry.published_at} /></p>}
        </li>
      ))}
    </ul>}
    <LoadMore page={page} label="published entries" />
  </>
}

function PublishedEntry({ id }) {
  const [params] = useSearchParams()
  const path = publicId.test(id) ? `${indexPath}/${id}` : null
  const entry = useResource(path)
  const data = entry.data
  useEffect(() => {
    if (data?.title) document.title = `${data.title} — Published Knowledge`
  }, [data?.title])

  return <>
    <Link to={queryPath(indexPath, { q: params.get('q') })}>All published entries</Link>
    {!path || entry.error?.status === 404 ? <h1>Published entry unavailable</h1> : <>
      <ResourceStatus resource={entry} label="published entry" />
      {data && <article className="panel">
        <h1>{data.title}</h1>
        {data.published_at && <p className="knowledge-last-activity">Published <Timestamp at={data.published_at} /></p>}
        <Markdown text={data.body} links={false} />
        {data.attachments?.length > 0 && <ul aria-label="Published attachments">
          {data.attachments.filter(file => publicId.test(file.id)).map(file => (
            <li key={file.id}><a href={`./api/v1${path}/attachments/${file.id}`}>{file.name}</a></li>
          ))}
        </ul>}
      </article>}
    </>}
  </>
}

export default function PublicKnowledge() {
  const { id } = useParams()
  useEffect(() => {
    document.title = 'Published Knowledge — Taskboard'
  }, [id])
  return <main aria-label="Published Knowledge">
    <section className="main-content knowledge">
      {id ? <PublishedEntry key={id} id={id} /> : <PublishedIndex />}
    </section>
  </main>
}
