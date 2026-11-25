import React, { useEffect, useId, useRef, useState } from 'react'
import { BoardLink, useBoardNavigation } from './BoardNavigation.jsx'
import { Search } from 'lucide-react'
import { queryPath, useLoadedPages, useResource } from './data.js'
import { LoadMore, ResourceStatus, TaskLink, taskHref } from './shared.jsx'
import { MarkdownInline } from './Markdown.jsx'
import { TaskRows } from './WorkRow.jsx'
import { matchesBoardShortcut } from './shortcuts.js'

export function TaskSearch({ query, fragment }) {
  const navigate = useBoardNavigation()
  const [draft, setDraft] = useState(query)
  const [openedOn, setOpenedOn] = useState(null)
  const expanded = openedOn === fragment
  if (openedOn !== null && !expanded) {
    setOpenedOn(null)
    setDraft(query)
  }
  const formId = useId()
  const listId = `${formId}-matches`
  const [suggesting, setSuggesting] = useState(false)
  const [active, setActive] = useState(-1)
  const [debounced, setDebounced] = useState('')
  const needle = suggesting ? draft.trim() : ''
  useEffect(() => {
    if (needle.length < 2) return
    const timer = window.setTimeout(() => setDebounced(needle), 180)
    return () => window.clearTimeout(timer)
  }, [needle])
  const suggestions = useResource(needle.length >= 2 && needle === debounced
    ? queryPath('/tasks/suggestions', { q: needle, scope: 'all' }) : null)
  const matches = suggestions.data?.items || []
  const activeMatch = matches[active]

  function hideSuggestions() {
    setSuggesting(false)
    setActive(-1)
  }

  function suggestionHref(match) {
    const context = new URLSearchParams({ 'from-search': needle })
    return `${taskHref(match.id, match.key)}?${context}`
  }

  const form = useRef(null)
  const toggle = useRef(null)
  const input = useRef(null)
  useEffect(() => {
    setDraft(query)
    setSuggesting(false)
    setActive(-1)
  }, [fragment, query])
  useEffect(() => {
    if (expanded) input.current?.focus()
  }, [expanded])
  useEffect(() => {
    function searchShortcut(event) {
      if (!matchesBoardShortcut(event, 'KeyS')) return
      event.preventDefault()
      if (!expanded && toggle.current.getClientRects().length) setOpenedOn(fragment)
      else input.current.focus()
    }
    window.addEventListener('keydown', searchShortcut)
    return () => window.removeEventListener('keydown', searchShortcut)
  }, [expanded, fragment])

  useEffect(() => {
    if (!suggesting) return
    function dismissOutside(event) {
      if (!form.current?.contains(event.target)) hideSuggestions()
    }
    document.addEventListener('pointerdown', dismissOutside, true)
    return () => document.removeEventListener('pointerdown', dismissOutside, true)
  }, [suggesting])

  function closeSearch() {
    setOpenedOn(null)
    toggle.current?.focus()
  }

  function openSuggestion(match) {
    const href = suggestionHref(match)
    hideSuggestions()
    if (expanded) closeSearch()
    navigate(href)
  }

  function dismissSearch() {
    hideSuggestions()
    setDraft(query)
    closeSearch()
  }

  function submit(event) {
    event.preventDefault()
    const search = draft.trim()
    if (search) {
      hideSuggestions()
      setDraft(search)
      if (expanded) closeSearch()
      navigate(new URLSearchParams({ search }).toString())
    }
  }

  function searchKey(event) {
    if (event.nativeEvent.isComposing) return
    if (event.key === 'Escape' && (suggesting || expanded)) {
      event.preventDefault()
      if (suggesting) hideSuggestions()
      else dismissSearch()
    } else if (event.target === input.current && matches.length) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        setActive(index => event.key === 'ArrowDown' ? Math.min(index + 1, matches.length - 1) : Math.max(index - 1, -1))
      } else if (event.key === 'Enter' && activeMatch) {
        event.preventDefault()
        openSuggestion(activeMatch)
      }
    }
  }

  return (
    <>
      <button ref={toggle} type="button" className="task-search-toggle" aria-label="Search tasks"
        aria-keyshortcuts="Alt+Shift+S" aria-expanded={expanded} aria-controls={formId}
        onClick={expanded ? dismissSearch : () => setOpenedOn(fragment)}>
        <Search size={19} aria-hidden="true" focusable="false" />
      </button>
      <form ref={form} id={formId} className="task-search" role="search" onSubmit={submit}
        data-expanded={expanded} data-reload-blocked={draft !== query} onKeyDown={searchKey}
        onBlur={event => {
          if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) hideSuggestions()
        }}>
        <input ref={input} type="search" role="combobox" aria-label="Search tasks" aria-keyshortcuts="Alt+Shift+S"
          aria-autocomplete="list"
          aria-expanded={matches.length > 0} aria-controls={matches.length > 0 ? listId : undefined}
          aria-activedescendant={activeMatch ? `${listId}-${active}` : undefined}
          placeholder="Search tasks" maxLength={160} required value={draft}
          onChange={event => {
            setDraft(event.target.value)
            setSuggesting(true)
            setActive(-1)
          }} />
        <button type="submit">Search</button>
        {matches.length > 0 && (
          <ul id={listId} className="search-suggestions" role="listbox" aria-label="Matching tickets">
            {matches.map((match, index) => (
              <li key={match.id} role="presentation">
                <BoardLink id={`${listId}-${index}`} role="option" aria-selected={index === active} tabIndex={-1}
                  href={suggestionHref(match)}
                  onClick={event => {
                    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
                    event.preventDefault()
                    openSuggestion(match)
                  }}>
                  <span className="ticket-id">{match.key}</span> <MarkdownInline text={match.title} links={false} />
                </BoardLink>
              </li>
            ))}
          </ul>
        )}
        {suggestions.data?.items.length === 0 && <p className="search-suggestions" role="status">No matching tickets.</p>}
      </form>
    </>
  )
}

export function SearchResults({ query, revision }) {
  const page = useLoadedPages('/tasks', { q: query, scope: 'all' }, revision)
  const items = page.data?.items
  const checksSuggestions = items?.length === 0 && query.length >= 2
  const suggestions = useResource(checksSuggestions
    ? queryPath('/tasks/suggestions', { q: query, scope: 'all' }) : null, revision)
  const matches = suggestions.data?.items || []

  return (
    <section className="search-results" aria-labelledby="search-results">
      <div className="page-heading">
        <h1 id="search-results">Results for “{query}”</h1>
      </div>
      <ResourceStatus resource={page} label="search results" />
      {checksSuggestions && <ResourceStatus resource={suggestions} label="suggested matches" />}
      {items?.length === 0 && (!checksSuggestions || suggestions.data) && matches.length === 0 && (
        <p className="section-message" role="status">No tasks match your search.</p>
      )}
      {matches.length > 0 && (
        <section aria-labelledby="suggested-results">
          <h2 id="suggested-results">Suggested matches</h2>
          <ul className="task-rows">
            {matches.map(match => (
              <li key={match.id}><TaskLink id={match.id} reference={match.key} title={match.title} searchQuery={query} /></li>
            ))}
          </ul>
        </section>
      )}
      <TaskRows items={items} label="Matching tasks" searchQuery={query} />
      <LoadMore page={page} label="search results" />
    </section>
  )
}
