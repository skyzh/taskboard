import React, { useState } from 'react'
import { queryPath, request, useResource } from './data.js'
import { ResourceStatus } from './shared.jsx'

function PublicationForm({ path, publication, pending, onCancel, onSave }) {
  const [title, setTitle] = useState(publication?.title ?? '')
  const [body, setBody] = useState(publication?.body ?? '')
  const [files, setFiles] = useState(publication?.attachments ?? [])
  const [source, setSource] = useState('')
  const [lookup, setLookup] = useState('')
  const candidates = useResource(lookup ? queryPath(path, { attachment_source: lookup }) : null)
  const selected = new Set(files.map(file => file.id))

  return (
    <form className="knowledge-form" onSubmit={event => {
      event.preventDefault()
      onSave({ title: title.trim(), body: body.trim(), attachment_ids: [...selected] })
    }}>
      <p>The title, text, and selected files will be available without signing in.</p>
      <label>Public title
        <input required maxLength={240} value={title} onChange={event => setTitle(event.target.value)} />
      </label>
      <label>Public text
        <textarea required maxLength={40000} rows={8} value={body} onChange={event => setBody(event.target.value)} />
      </label>
      <details>
        <summary>Choose attachments</summary>
        <p>Choose files from a ticket you can edit in this project.</p>
        <label>Source ticket
          <input value={source} maxLength={30} onChange={event => setSource(event.target.value)} />
        </label>
        <button type="button" disabled={!source.trim() || pending} onClick={() => {
          if (lookup === source.trim()) candidates.retry()
          else setLookup(source.trim())
        }}>Find attachments</button>
        <ResourceStatus resource={candidates} label="source attachments" />
        {candidates.data?.attachments.length === 0 && <p>No attachments found.</p>}
        {!candidates.pending && !candidates.error && candidates.data?.attachments
          .filter(file => !selected.has(file.id)).map(file => (
            <button key={file.id} type="button" disabled={pending || files.length >= 50}
              onClick={() => setFiles(previous => [...previous, file])}>Add {file.filename}</button>
          ))}
      </details>
      {files.length > 0 && (
        <ul aria-label="Files selected for publication">
          {files.map(file => (
            <li key={file.id}>
              {file.filename} <button type="button" disabled={pending} aria-label={`Remove ${file.filename}`}
                onClick={() => setFiles(previous => previous.filter(item => item.id !== file.id))}>Remove</button>
            </li>
          ))}
        </ul>
      )}
      <div className="attachment-actions">
        <button type="submit" disabled={pending || !title.trim() || !body.trim()}>
          {publication ? 'Update publication' : 'Publish publicly'}
        </button>
        <button type="button" onClick={onCancel} disabled={pending}>Cancel</button>
      </div>
    </form>
  )
}

export default function KnowledgePublication({ ticket, revision, onChanged }) {
  const path = `/knowledge/${encodeURIComponent(ticket)}/publication`
  const result = useResource(path, revision)
  const [editing, setEditing] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [pending, setPending] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const publication = result.data?.publication

  if ([403, 404].includes(result.lastError?.status)) return null

  async function save(method, body) {
    setPending(true)
    setNotice('')
    setError('')
    try {
      await request(path, { method, body })
      setEditing(false)
      setConfirming(false)
      setNotice(method === 'DELETE' ? 'Publication withdrawn.' : 'Publication saved.')
      result.retry()
      onChanged?.()
    } catch (failure) {
      setError(failure.message)
    } finally {
      setPending(false)
    }
  }

  return (
    <section className="knowledge-create" aria-label="Public publication" data-reload-blocked={editing || confirming || pending}>
      <h3>Public publication</h3>
      <ResourceStatus resource={result} label="publication" />
      {result.data && !result.lastError && (
        <>
          {publication
            ? <p><a href={publication.url}>Open published entry</a></p>
            : <p>This entry is unpublished.</p>}
          {editing
            ? <PublicationForm path={path} publication={publication} pending={pending}
                onCancel={() => { setEditing(false); setError('') }} onSave={body => save('PUT', body)} />
            : <>
                <button type="button" disabled={result.pending || pending || confirming}
                  onClick={() => { setEditing(true); setNotice('') }}>
                  {publication ? 'Edit publication' : 'Prepare publication'}
                </button>
                {publication && (confirming
                  ? <div>
                      <p>The public entry and its file links will become unavailable.</p>
                      <button type="button" disabled={pending} onClick={() => save('DELETE')}>Confirm unpublish</button>
                      <button type="button" disabled={pending} onClick={() => setConfirming(false)}>Keep published</button>
                    </div>
                  : <button type="button" disabled={result.pending || pending} onClick={() => {
                      setConfirming(true)
                      setNotice('')
                    }}>Unpublish</button>)}
              </>}
        </>
      )}
      {notice && <p role="status">{notice}</p>}
      {error && <p role="alert">{error}</p>}
    </section>
  )
}
