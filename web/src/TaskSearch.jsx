import React, { useEffect, useId, useRef, useState } from 'react'
import { Search } from 'lucide-react'
import { useLoadedPages } from './data.js'
import { LoadMore, ResourceStatus } from './shared.jsx'
import { TaskRows } from './WorkRow.jsx'

export function TaskSearch({ query, fragment }) {
  const [draft, setDraft] = useState(query)
  const [openedOn, setOpenedOn] = useState(null)
  const expanded = openedOn === fragment
  if (openedOn !== null && !expanded) {
    setOpenedOn(null)
    setDraft(query)
  }
  const formId = useId()
  const toggle = useRef(null)
  const input = useRef(null)
  useEffect(() => setDraft(query), [query])
  useEffect(() => {
    if (expanded) input.current?.focus()
  }, [expanded])

  function closeSearch() {
    setOpenedOn(null)
    toggle.current?.focus()
  }

  function dismissSearch() {
    setDraft(query)
    closeSearch()
  }

  function submit(event) {
    event.preventDefault()
    const search = draft.trim()
    if (search) {
      setDraft(search)
      if (expanded) closeSearch()
      window.location.hash = new URLSearchParams({ search }).toString()
    }
  }

  return (
    <>
      <button ref={toggle} type="button" className="task-search-toggle" aria-label="Search tasks"
        aria-expanded={expanded} aria-controls={formId} onClick={expanded ? dismissSearch : () => setOpenedOn(fragment)}>
        <Search size={19} aria-hidden="true" focusable="false" />
      </button>
      <form id={formId} className="task-search" role="search" onSubmit={submit}
        data-expanded={expanded} data-reload-blocked={draft !== query}
        onKeyDown={event => {
          if (expanded && event.key === 'Escape') {
            event.preventDefault()
            dismissSearch()
          }
        }}>
        <input ref={input} type="search" aria-label="Search tasks" placeholder="Search tasks" maxLength={160} required
          value={draft} onChange={event => setDraft(event.target.value)} />
        <button type="submit">Search</button>
      </form>
    </>
  )
}

export function SearchResults({ query, revision }) {
  const page = useLoadedPages('/tasks', { q: query, scope: 'all' }, revision)
  const items = page.data?.items

  return (
    <section className="search-results" aria-labelledby="search-results">
      <div className="page-heading">
        <h1 id="search-results">Results for “{query}”</h1>
      </div>
      <ResourceStatus resource={page} label="search results" />
      {items?.length === 0 && <p className="section-message" role="status">No tasks match your search.</p>}
      <TaskRows items={items} label="Matching tasks" />
      <LoadMore page={page} label="search results" />
    </section>
  )
}
