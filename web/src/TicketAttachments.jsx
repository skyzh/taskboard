import React, { useRef, useState } from 'react'
import { request, useResource } from './data.js'
import { Timestamp, profileName } from './shared.jsx'

const storageUnavailable = 'attachment_storage_unavailable'
const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 })

function size(bytes) {
  if (bytes >= 1024 ** 2) return `${number.format(bytes / 1024 ** 2)} MiB`
  if (bytes >= 1024) return `${number.format(bytes / 1024)} KiB`
  return `${number.format(bytes)} B`
}

export default function TicketAttachments({ ticket, revision, onChanged }) {
  const path = `/tasks/${encodeURIComponent(ticket)}/attachments`
  const list = useResource(path, revision, { localErrorCode: storageUnavailable })
  const input = useRef(null)
  const [selection, setSelection] = useState(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [confirming, setConfirming] = useState(null)
  const data = list.lastError ? null : list.data
  const editable = data?.can_edit && !list.pending
  const tooLarge = selection && selection.file.size > data?.max_upload_bytes

  function select(file) {
    setSelection(file ? { file, id: crypto.randomUUID() } : null)
    setError('')
  }

  async function save(action, clear) {
    setPending(true)
    setError('')
    try {
      await action()
      clear()
      list.retry()
      onChanged?.()
    } catch (failure) {
      if ([401, 403, 404].includes(failure.status)) list.retry()
      if (failure.code === 'upload_id_conflict') {
        clear()
        setError('This upload ID is already in use. Select the file again to start a new upload.')
      } else {
        setError(failure.message)
      }
    } finally {
      setPending(false)
    }
  }

  function upload(event) {
    event.preventDefault()
    if (!editable || pending || !selection || tooLarge) return
    const body = new FormData()
    body.set('file', selection.file)
    body.set('upload_id', selection.id)
    save(() => request(path, { body }), () => {
      select(null)
      if (input.current) input.current.value = ''
    })
  }

  return (
    <section className="ticket-attachments" aria-label="Attachments" data-reload-blocked={pending || Boolean(selection)}>
      <h2>Attachments</h2>
      {list.pending && !data && <p role="status">Loading attachments…</p>}
      {list.error && (
        <div className="section-message" role="status">
          <p>{list.error.code === storageUnavailable ? 'Attachment storage is unavailable.' : list.error.message}</p>
          <button type="button" onClick={list.retry}>Retry loading attachments</button>
        </div>
      )}
      {data && <>
        {!data.attachments.length && <p>No attachments yet.</p>}
        {data.attachments.length > 0 && <ul className="attachment-list" role="list">
          {data.attachments.map(attachment => (
            <li key={attachment.id}>
              <a href={attachment.download_url} download={attachment.filename} aria-label={`Download ${attachment.filename}`}>{attachment.filename}</a>
              <span className="event-meta">{size(attachment.size_bytes)} · {profileName(attachment.created_by)} · <Timestamp at={attachment.created_at} /></span>
              {data.can_edit && (confirming === attachment.id ? <div className="attachment-actions">
                <button type="button" disabled={pending || !editable} onClick={() =>
                  save(() => request(`${path}/${encodeURIComponent(attachment.id)}`, { method: 'DELETE' }), () => setConfirming(null))
                }>Confirm removal</button>
                <button type="button" disabled={pending} onClick={() => setConfirming(null)}>Keep file</button>
              </div> : <button type="button" disabled={pending || !editable} aria-label={`Remove ${attachment.filename}`}
                onClick={() => { setConfirming(attachment.id); setError('') }}>Remove</button>)}
            </li>
          ))}
        </ul>}
        {data.can_edit && <form className="attachment-actions" onSubmit={upload}>
          <label>Choose an attachment
            <input type="file" ref={input} disabled={!editable || pending}
              onChange={event => select(event.target.files?.[0])} />
          </label>
          <button type="submit" disabled={!editable || pending || !selection || tooLarge}>Upload attachment</button>
          {selection && <span className="event-meta">{selection.file.name}</span>}
          <span className="event-meta">Maximum file size {size(data.max_upload_bytes)}</span>
        </form>}
      </>}
      {tooLarge && <p role="alert">Choose a file within the upload limit.</p>}
      {error && <p role="alert">{error}</p>}
    </section>
  )
}
