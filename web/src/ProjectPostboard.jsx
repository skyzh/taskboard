import React, { useId, useState } from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { request, useLoadedPages, useResource } from './data.js'
import { Markdown } from './Markdown.jsx'
import { LoadMore, ResourceStatus, Timestamp } from './shared.jsx'

function Revision({ value }) {
  return <>
    <Markdown text={value.content} links={false} />
    {value.sources.length > 0 && <ul aria-label="Source tickets">
      {value.sources.map(source => <li key={source.id}><BoardLink href={source.url}>{source.key} · {source.title}</BoardLink></li>)}
    </ul>}
  </>
}

function RevisionHistory({ path, revision }) {
  const [open, setOpen] = useState(false)
  const page = useLoadedPages(`${path}/revisions`, {}, revision, { enabled: open })
  return <details className="report-details" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>Revision history</summary>
    {open && <>
      <ResourceStatus resource={page} label="postboard history" />
      {page.data?.items.map(item => <details className="report-details" key={item.revision_id}>
        <summary><Timestamp at={item.updated_at} /></summary>
        <Revision value={item} />
      </details>)}
      <LoadMore page={page} label="postboard revisions" />
    </>}
  </details>
}

function EditRevision({ current, path, project, onSaved, onConflict, onCancel }) {
  const sourceHelp = useId()
  const [content, setContent] = useState(current?.content || '')
  const [sources, setSources] = useState(current?.sources.map(source => source.key).join(', ') || '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [stale, setStale] = useState(false)

  async function submit(event) {
    event.preventDefault()
    setSaving(true)
    setError(null)
    try {
      const source_ticket_ids = [...new Set(sources.toUpperCase().split(/[ ,\s]+/).filter(Boolean))]
      await request(`${path}/revisions`, {
        body: { content, source_ticket_ids, expected_revision_id: current?.revision_id ?? null },
      })
      onSaved()
    } catch (failure) {
      if (failure.status === 409) {
        setStale(true)
        setError('A newer revision is available. The latest version is shown above and your draft is still here.')
        onConflict()
      } else {
        setError(failure.message)
      }
    } finally {
      setSaving(false)
    }
  }

  return <form className="profile-form dri-goal-form" onSubmit={submit} data-reload-blocked="true">
    <label>Diagram and notes<textarea value={content} rows={8} maxLength={8000} required disabled={saving}
      onChange={event => setContent(event.target.value)} /></label>
    <label>Source tickets<input value={sources} aria-describedby={sourceHelp} disabled={saving}
      onChange={event => setSources(event.target.value)} /></label>
    <p id={sourceHelp} className="section-message">Enter ticket keys from {project}, separated by commas.</p>
    <div className="dri-goal-actions">
      <button disabled={saving || stale}>{saving ? 'Saving…' : 'Save revision'}</button>
      <button type="button" disabled={saving} onClick={onCancel}>Cancel</button>
    </div>
    {error && <p role="alert">{error}</p>}
  </form>
}

export default function ProjectPostboard({ owner, project, revision }) {
  const path = `/projects/${encodeURIComponent(project)}/postboards/${encodeURIComponent(owner)}`
  const resource = useResource(path, revision)
  const board = resource.data
  const [draft, setDraft] = useState(null)
  const [localRevision, setLocalRevision] = useState(0)

  return <section className="feed-section" aria-labelledby="postboard-title">
    <h2 id="postboard-title" tabIndex={-1}>Postboard</h2>
    <ResourceStatus resource={resource} label="postboard" />
    {board && <>
      {board.current ? <>
        <p className="section-message">Updated <Timestamp at={board.current.updated_at} /></p>
        <Revision value={board.current} />
      </> : <p className="section-message">No postboard revision yet.</p>}
      {draft && board.can_edit && <EditRevision current={draft.current} path={path} project={project}
        onCancel={() => setDraft(null)} onConflict={resource.retry} onSaved={() => {
          setDraft(null)
          setLocalRevision(value => value + 1)
          resource.retry()
        }} />}
      {!draft && board.can_edit && <button type="button" onClick={() => setDraft({ current: board.current })}>
        {board.current ? 'Edit postboard' : 'Write postboard'}
      </button>}
      {board.current && <RevisionHistory path={path} revision={`${revision}:${localRevision}`} />}
    </>}
  </section>
}
