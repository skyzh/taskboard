import React, { useState } from 'react'
import { request, useLoadedPages, useResource } from './data.js'
import { Markdown, MarkdownInline } from './Markdown.jsx'
import RecordHistory, { historyHref } from './RecordHistory.jsx'
import { LoadMore, ResourceStatus, eventHref, signInHref, taskHref } from './shared.jsx'

export function entryHref(key) {
  return '#knowledge?' + new URLSearchParams({ entry: key })
}

function KnowledgeCreate({ projects, onCreated }) {
  const [title, setTitle] = useState('')
  const [brief, setBrief] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState(null)

  async function submit(event) {
    event.preventDefault()
    if (pending || !title.trim() || !brief.trim()) return
    const project = new FormData(event.currentTarget).get('project')
    setError(null)
    setPending(true)
    try {
      const { id } = await request('/knowledge', {
        body: { project, title: title.trim(), brief: brief.trim() },
      })
      onCreated()
      window.location.hash = entryHref(id)
    } catch (failure) {
      setError(failure.message)
    } finally {
      setPending(false)
    }
  }

  return (
    <details className="panel knowledge-create">
      <summary>New Knowledge entry</summary>
      <form className="ownership-form" onSubmit={submit}
        data-reload-blocked={Boolean(pending || title || brief)}>
        <label>Project
          <select name="project" disabled={pending}>
            {projects.map(project => <option key={project.prefix} value={project.prefix}>{project.title}</option>)}
          </select>
        </label>
        <label>Title
          <input value={title} onChange={event => setTitle(event.target.value)} maxLength={4000} required disabled={pending} />
        </label>
        <label>Brief
          <textarea value={brief} onChange={event => setBrief(event.target.value)}
            rows={5} maxLength={4000} required disabled={pending} />
        </label>
        {error && <p role="alert">{error}</p>}
        <button type="submit" disabled={pending || !title.trim() || !brief.trim()}>
          {pending ? 'Creating…' : 'Create entry'}
        </button>
      </form>
    </details>
  )
}

function KnowledgeSearch({ search, onSearch }) {
  const [draft, setDraft] = useState(search)
  return (
    <form className="knowledge-search" role="search" onSubmit={event => {
      event.preventDefault()
      onSearch(draft.trim())
    }}>
      <label>
        Search Knowledge
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

function AreaBranches({ areas, parent = '' }) {
  const children = areas.filter(area => area.path.split('/').slice(0, -1).join('/') === parent)
  if (!children.length) return null
  return (
    <ul>
      {children.map(area => (
        <li key={area.path}>
          <a href={area.available ? entryHref(area.index) : taskHref(area.index)}>{area.path.split('/').at(-1)}</a>
          {!area.available && <span className="knowledge-stage-state">Index under review</span>}
          <AreaBranches areas={areas} parent={area.path} />
        </li>
      ))}
    </ul>
  )
}

function KnowledgeDirectory({ directory, projects }) {
  const projectTitles = new Map(projects?.map(({ prefix, title }) => [prefix, title]))
  const projectAreas = new Map()
  for (const area of directory.data?.areas || []) {
    const areas = projectAreas.get(area.project) || []
    areas.push(area)
    projectAreas.set(area.project, areas)
  }
  return (
    <>
      <ResourceStatus resource={directory} label="areas" />
      {projectAreas.size > 0 && (
        <nav className="panel knowledge-directory" aria-label="Knowledge areas">
          <h2>Areas</h2>
          {[...projectAreas].map(([project, areas]) => {
            const title = projectTitles.get(project) || project
            return (
              <section key={project} aria-label={title}>
                <h3>{title}</h3>
                <AreaBranches areas={areas} />
              </section>
            )
          })}
        </nav>
      )}
    </>
  )
}

export function StagedWork({ changes }) {
  return changes?.length > 0 && (
    <section className="knowledge-stages" aria-label="Staged work">
      <h3>Staged work</h3>
      <ul className="context-list" role="list">
        {changes.map(change => (
          <li key={change.source} data-staged-at={change.staged_at} data-source-at={change.source_at}>
            <a href={taskHref(change.source)}>{change.source}</a> <MarkdownInline text={change.source_title} links={false} />
            <span className="knowledge-stage-state">
              {change.applied_event_id ? <a href={eventHref(change.applied_event_id)}>Applied</a>
                : change.conflict ? change.conflict : 'Staged'}
            </span>
            {change.source_event_id && <a href={eventHref(change.source_event_id)}>Source event</a>}
          </li>
        ))}
      </ul>
    </section>
  )
}

export default function Knowledge({ session, verified, revision, projects, search, entry, history, onSearch, onChanged, RecordTools }) {
  const page = useLoadedPages(verified ? '/knowledge' : null, { q: entry ? undefined : search, entry }, revision)
  const directory = useResource(verified && !entry && !search ? '/knowledge/areas' : null, revision)
  const signIn = session.data?.sign_in_available || session.error?.status === 401
  const fragment = entry ? entryHref(entry) : search ? '#knowledge?' + new URLSearchParams({ q: search }) : '#knowledge'
  const writableProjects = projects?.filter(project => project.can_create_knowledge === true) || []
  if (history && entry && verified && page.data?.items.length > 0) {
    return (
      <article className="panel task-detail" id="knowledge" tabIndex={-1} aria-label="Knowledge history">
        <RecordHistory record={entry} currentHref={fragment} history={history} revision={revision} knowledge />
      </article>
    )
  }
  return (
    <section className="knowledge" aria-labelledby="knowledge">
      <div className="page-heading"><h1 id="knowledge" tabIndex={-1}>Knowledge</h1>
        {entry && <a href="#knowledge">All entries</a>}
      </div>
      {verified ? <>
        {!entry && writableProjects.length > 0 &&
          <KnowledgeCreate key={session.data?.actor?.id} projects={writableProjects} onCreated={onChanged} />}
        <KnowledgeSearch key={search} search={search} onSearch={onSearch} />
        {!entry && !search && <KnowledgeDirectory directory={directory} projects={projects} />}
        <ResourceStatus resource={page} label="Knowledge entries" />
        {page.data?.items.length === 0 && <p className="section-message">
          {search || entry ? 'No matching entries.' : 'No Knowledge entries yet.'}
        </p>}
        {page.data?.items.length > 0 && (
          <ol className="knowledge-list" aria-label="Knowledge entries" role="list">
            {page.data.items.map(item => (
              <li className="panel" key={item.id}>
                <h2><a href={entryHref(item.key)}><MarkdownInline text={item.title} links={false} /></a></h2>
                <a className="ticket-id" href={taskHref(item.key)}>{item.key}</a>
                {entry && <a className="history-link" href={historyHref(entryHref(item.key))}>History</a>}
                {item.area && <p className="knowledge-area-label">{item.area}</p>}
                {item.summary && <Markdown text={item.summary} />}
                <DecisionSources sources={item.sources} />
                <StagedWork changes={item.staged_work} />
                {entry && item.area_entries?.length > 0 && (
                  <section className="knowledge-area-entries">
                    <h3 id={`knowledge-area-entries-${item.id}`}>Entries in this area</h3>
                    <ul className="context-list" aria-labelledby={`knowledge-area-entries-${item.id}`} role="list">
                      {item.area_entries.map(note => (
                        <li key={note.key}><a href={entryHref(note.key)}>
                          <MarkdownInline text={note.title} links={false} />
                        </a></li>
                      ))}
                    </ul>
                  </section>
                )}
                {entry && RecordTools && <RecordTools id={item.key} revision={revision} onChanged={onChanged} />}
              </li>
            ))}
          </ol>
        )}
        <LoadMore page={page} label="Knowledge entries" />
      </> : signIn ? <a href={signInHref(history && entry ? historyHref(fragment, history) : fragment)}>Sign in to view Knowledge</a>
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
        {page.data?.items.map(item => {
          const stage = item.staged_work?.find(change => change.source === ticket)
          return (
            <li key={item.id}>
              <a href={entryHref(item.key)}><MarkdownInline text={item.title} links={false} /></a>
              {stage && <span className="knowledge-stage-state">
                {stage.applied_event_id ? 'Applied' : stage.conflict ? 'Needs review' : 'Staged work'}
              </span>}
            </li>
          )
        })}
      </ul>
      <LoadMore page={page} label="linked decisions" />
    </section>
  )
}
