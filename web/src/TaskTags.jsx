import React, { useState } from 'react'
import { request } from './data.js'

const tagName = /^[a-z0-9]+(?:-[a-z0-9]+)*$/
const maxLength = 32

export default function TaskTags({ task, editable, canTagDecisionRecord, onChanged }) {
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState(null)
  const tags = task.tags || []
  if (!editable) return null

  async function save(tag, action) {
    if (saving) return
    setSaving(true)
    setNotice(null)
    try {
      await request(`/tasks/${encodeURIComponent(task.key || task.id)}/tags`, {
        method: 'POST', body: { tag, action },
      })
      if (action === 'add') setDraft('')
      setNotice({ text: action === 'add' ? 'Tag added.' : 'Tag removed.' })
      onChanged()
    } catch (error) {
      setNotice({ error: true, text: error.message })
    } finally {
      setSaving(false)
    }
  }

  function add(event) {
    event.preventDefault()
    const tag = draft.trim().toLowerCase()
    if (!tagName.test(tag) || tag.length > maxLength) {
      setNotice({ error: true, text: 'Use 1 to 32 letters or digits, with single hyphens between words.' })
      return
    }
    if (tag === 'decision-record' && !canTagDecisionRecord) {
      setNotice({ error: true, text: 'The decision-record tag is reserved for Knowledge classification.' })
      return
    }
    return save(tag, 'add')
  }

  return (
    <section className="task-tags" aria-label="Task tags">
      {tags.length > 0 && (
        <ul className="task-tag-list" role="list">
          {tags.map(tag => (
            <li key={tag}>
              <span>{tag}</span>
              {(tag !== 'decision-record' || canTagDecisionRecord) &&
                <button type="button" disabled={saving} aria-label={`Remove tag ${tag}`}
                  onClick={() => save(tag, 'remove')}>Remove</button>}
            </li>
          ))}
        </ul>
      )}
      <form className="task-tag-form" onSubmit={add} data-reload-blocked={Boolean(saving || draft)}>
        <label>Tag
          <input type="text" value={draft} maxLength={maxLength} required disabled={saving}
            autoCapitalize="none" spellCheck={false}
            onChange={event => { setDraft(event.target.value); setNotice(null) }} />
        </label>
        <button type="submit" disabled={saving || !draft.trim()}>Add tag</button>
      </form>
      {notice && <p role={notice.error ? 'alert' : 'status'}>{notice.text}</p>}
    </section>
  )
}
