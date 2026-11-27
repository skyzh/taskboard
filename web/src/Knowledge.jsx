import React, { useState } from 'react'
import { BoardLink, useBoardNavigation, useContentTitle } from './BoardNavigation.jsx'
import { queryPath, request, useLoadedPages, useResource } from './data.js'
import EditToggle from './EditToggle.jsx'
import { Markdown, MarkdownInline } from './Markdown.jsx'
import RecordHistory, { historyHref } from './RecordHistory.jsx'
import SegmentedControl from './SegmentedControl.jsx'
import { ReportTime } from './ReportTime.jsx'
import { LoadMore, ResourceStatus, eventHref, useSignInHref, taskHref } from './shared.jsx'

export function entryHref(key) {
  return '#knowledge?' + new URLSearchParams({ entry: key })
}

export function knowledgeListHref(scope, search, { order, project } = {}) {
  const params = new URLSearchParams()
  if (scope === 'assigned') params.set('scope', scope)
  if (search) params.set('q', search)
  if (order === 'updated') params.set('order', order)
  if (project) params.set('project', project)
  return params.size ? `#knowledge?${params}` : '#knowledge'
}

const knowledgeScopes = [
  { value: 'board', label: 'All entries' },
  { value: 'assigned', label: 'Assigned to me' },
]

function KnowledgeCreate({ projects, onCreated }) {
  const navigate = useBoardNavigation()
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
      navigate(entryHref(id))
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
          <input value={title} onChange={event => setTitle(event.target.value)} required disabled={pending} />
        </label>
        <label>Brief
          <textarea value={brief} onChange={event => setBrief(event.target.value)}
            rows={5} required disabled={pending} />
        </label>
        {error && <p role="alert">{error}</p>}
        <button type="submit" disabled={pending || !title.trim() || !brief.trim()}>
          {pending ? 'Creating…' : 'Create entry'}
        </button>
      </form>
    </details>
  )
}

function MaintenanceDriForm({ item, revision, onChanged }) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(null)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState(null)
  const people = useLoadedPages(open ? '/assignees' : null, { project: item.project }, revision)
  const current = item.maintenance_dri
  const currentId = current?.id || ''
  const selection = draft ?? currentId
  const choices = (people.data?.items || []).filter(person => person.id && person.id !== currentId)

  async function submit(event) {
    event.preventDefault()
    if (saving || selection === currentId) return
    setSaving(true)
    setNotice(null)
    try {
      await request(`/knowledge/${encodeURIComponent(item.key)}/maintenance-dri`, {
        body: { dri: selection || null },
      })
      setDraft(null)
      setNotice({ text: 'Maintenance DRI updated.' })
      onChanged()
    } catch (error) {
      setNotice({ error: true, text: error.message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <details className="task-ownership knowledge-maintenance" onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>Change maintenance DRI</summary>
      <form className="ownership-form" onSubmit={submit} data-reload-blocked={saving || selection !== currentId}>
        <label>Maintenance DRI
          <select value={selection} disabled={saving} onChange={event => {
            setDraft(event.target.value)
            setNotice(null)
          }}>
            <option value="">Unassigned</option>
            {current && <option value={currentId}>{current.name}</option>}
            {choices.map(person => <option key={person.id} value={person.id}>{person.name || person.username}</option>)}
          </select>
        </label>
        <ResourceStatus resource={people} label="eligible people" />
        <LoadMore page={people} label="eligible people" />
        <button type="submit" disabled={saving || selection === currentId}>{saving ? 'Saving…' : 'Save maintenance DRI'}</button>
        {notice && <p role={notice.error ? 'alert' : 'status'}>{notice.text}</p>}
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
          <BoardLink href={taskHref(source.task_key)}>{source.task_key}</BoardLink>
          <BoardLink href={eventHref(source.event_id)}>{source.label}</BoardLink>
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
          <BoardLink href={area.available ? entryHref(area.index) : taskHref(area.index)}>{area.path.split('/').at(-1)}</BoardLink>
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
            <BoardLink href={taskHref(change.source)}>{change.source}</BoardLink> <MarkdownInline text={change.source_title} links={false} />
            <span className="knowledge-stage-state">
              {change.applied_event_id ? <BoardLink href={eventHref(change.applied_event_id)}>Applied</BoardLink>
                : change.conflict ? change.conflict : 'Staged'}
            </span>
            {change.source_event_id && <BoardLink href={eventHref(change.source_event_id)}>Source event</BoardLink>}
          </li>
        ))}
      </ul>
    </section>
  )
}

export default function Knowledge({ session, verified, revision, projects, search, entry, history, scope = 'board',
  preview, project, projectPicker, order = '', onSearch, onScope, onOrder, onChanged, RecordTools }) {
  const [editing, setEditing] = useState(false)
  const assigned = !entry && scope === 'assigned'
  const page = useLoadedPages(verified ? '/knowledge' : null, {
    q: entry ? undefined : search, entry, scope: assigned ? 'assigned' : undefined,
    project: entry ? undefined : project, order: !entry && order ? order : undefined,
  }, revision)
  const showDirectory = !entry && !search && !assigned
  const directory = useResource(verified && showDirectory ? queryPath('/knowledge/areas', { project }) : null, revision)
  const signIn = session.data?.sign_in_available || session.error?.status === 401
  const fragment = entry ? entryHref(entry) : knowledgeListHref(scope, search, { order, project })
  const login = useSignInHref(history && entry ? historyHref(fragment, history) : fragment)
  const writableProjects = projects?.filter(project => project.can_create_knowledge === true) || []
  const promptSettings = '#settings?' + new URLSearchParams({
    section: 'agent-prompts', prompt: 'knowledge', ...(project && { project }),
  })
  const entryTitle = verified && entry && page.data?.items[0]?.title
  useContentTitle(entryTitle ? `${entry} · ${entryTitle}` : null)
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
        {entry ? <BoardLink href={knowledgeListHref('board', '', { project })}>All entries</BoardLink> : projectPicker}
        {!entry && !preview && <a href="./public/knowledge">Published Knowledge</a>}
        {verified && !entry && writableProjects.length > 0 &&
          <EditToggle label="Edit Knowledge" active={editing} onToggle={() => setEditing(value => !value)} />}
      </div>
      {verified ? <>
        {!entry && writableProjects.length > 0 && <div hidden={!editing}>
          <KnowledgeCreate key={session.data?.actor?.id} projects={writableProjects} onCreated={onChanged} />
        </div>}
        {!entry && !preview && <SegmentedControl className="knowledge-scope" label="Knowledge entries to show"
          value={scope} options={knowledgeScopes} onChange={onScope} />}
        <div className="knowledge-browse-controls">
          <KnowledgeSearch key={search} search={search} onSearch={onSearch} />
          {!preview && <BoardLink href={promptSettings}>Knowledge prompt</BoardLink>}
          {!entry && !preview && onOrder && <label className="knowledge-order">
            Sort by
            <select value={order} onChange={event => onOrder(event.currentTarget.value)}>
              <option value="">Default order</option>
              <option value="updated">Last updated</option>
            </select>
          </label>}
        </div>
        {showDirectory && <KnowledgeDirectory directory={directory} projects={projects} />}
        <ResourceStatus resource={page} label="Knowledge entries" />
        {page.data?.items.length === 0 && <p className="section-message">
          {search || entry ? 'No matching entries.' : assigned ? 'No Knowledge entries assigned to you.' : 'No Knowledge entries yet.'}
        </p>}
        {page.data?.items.length > 0 && (
          <ol className="knowledge-list" aria-label="Knowledge entries" role="list">
            {page.data.items.map(item => (
              <li className="panel" key={item.id}>
                <h2><BoardLink href={entryHref(item.key)}><MarkdownInline text={item.title} links={false} /></BoardLink></h2>
                <BoardLink className="ticket-id" href={taskHref(item.key)}>{item.key}</BoardLink>
                {entry && <BoardLink className="history-link" href={historyHref(entryHref(item.key))}>History</BoardLink>}
                {!entry && item.last_activity && <p className="last-activity knowledge-last-activity">
                  Last activity {item.last_activity.label && <span>· {item.last_activity.label} </span>}
                  <BoardLink href={eventHref(item.last_activity.id)}><ReportTime at={item.last_activity.at} linked /></BoardLink>
                </p>}
                {item.area && <p className="knowledge-area-label">{item.area}</p>}
                {item.maintenance_dri !== undefined && <p className="knowledge-area-label">
                  Maintenance DRI <strong>{item.maintenance_dri?.name || 'Unassigned'}</strong>
                </p>}
                {entry && item.can_assign_maintenance_dri && !preview && <MaintenanceDriForm
                  key={`${session.data?.actor?.id}:${item.key}`} item={item} revision={revision} onChanged={onChanged} />}
                {item.summary && <Markdown text={item.summary} />}
                <DecisionSources sources={item.sources} />
                <StagedWork changes={item.staged_work} />
                {entry && item.area_entries?.length > 0 && (
                  <section className="knowledge-area-entries">
                    <h3 id={`knowledge-area-entries-${item.id}`}>Entries in this area</h3>
                    <ul className="context-list" aria-labelledby={`knowledge-area-entries-${item.id}`} role="list">
                      {item.area_entries.map(note => (
                        <li key={note.key}><BoardLink href={entryHref(note.key)}>
                          <MarkdownInline text={note.title} links={false} />
                        </BoardLink></li>
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
      </> : signIn ? <a href={login}>Sign in to view Knowledge</a>
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
              <BoardLink href={entryHref(item.key)}><MarkdownInline text={item.title} links={false} /></BoardLink>
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
