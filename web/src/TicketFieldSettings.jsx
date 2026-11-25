import React, { useState } from 'react'
import { request, useResource } from './data.js'
import { ResourceStatus } from './shared.jsx'

const types = { duration: 'Duration', money: 'Money', decimal: 'Decimal', text: 'Text' }

function DefinitionForm({ field, saving, onSave, onRemove }) {
  const [kind, setKind] = useState(field?.kind || 'duration')
  const [dirty, setDirty] = useState(false)

  function submit(event) {
    event.preventDefault()
    const values = new FormData(event.currentTarget)
    const definition = { key: values.get('fieldKey'), kind, label: values.get('label'), prompt: values.get('prompt') }
    if (kind === 'duration' && values.get('workerSource')) definition.source = 'recorded_workers'
    onSave(definition)
  }

  return (
    <form className="ownership-form" onChange={() => setDirty(true)} onSubmit={submit}
      data-reload-blocked={saving || dirty}>
      <label>Field key
        <input name="fieldKey" defaultValue={field?.key || ''} readOnly={Boolean(field)} disabled={saving}
          pattern="[a-z][a-z0-9_]{0,31}" maxLength={32} required />
      </label>
      <label>Label<input name="label" defaultValue={field?.label || ''} maxLength={64} disabled={saving} required /></label>
      <label>Type
        <select value={kind} disabled={saving || Boolean(field)} onChange={event => setKind(event.target.value)}>
          {Object.entries(types).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      {kind === 'duration' && <label>
        <span><input name="workerSource" type="checkbox" disabled={saving}
          defaultChecked={field?.source === 'recorded_workers'} /> Use recorded worker time when available</span>
      </label>}
      <label>Collection prompt
        <input name="prompt" defaultValue={field?.prompt || ''} maxLength={500} disabled={saving} required />
      </label>
      <div className="field-guidance-actions">
        <button type="submit" disabled={saving || !dirty}>{field ? 'Save field' : 'Add field'}</button>
        {field && <button type="button" disabled={saving} onClick={onRemove}>Remove field</button>}
      </div>
    </form>
  )
}

export default function TicketFieldSettings({ project, revision }) {
  const path = `/projects/${encodeURIComponent(project)}/ticket-fields`
  const resource = useResource(path, revision)
  const [selected, setSelected] = useState('')
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState(null)
  const fields = resource.data?.fields || []
  const field = fields.find(item => item.key === selected)
  if (resource.error?.status === 404) return null

  async function save(fields, selection) {
    setSaving(true)
    setResult(null)
    try {
      await request(path, { method: 'PUT', body: { fields } })
      setSelected(selection)
      setResult({ message: selection ? 'Field saved.' : 'Field removed.' })
      resource.retry()
    } catch (error) {
      setResult({ error: true, message: error.message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <section aria-label="Project ticket fields">
      <h3>Ticket fields</h3>
      <ResourceStatus resource={resource} label="project ticket fields" />
      {resource.data?.editable && <>
        <label className="settings-preference field-guidance-selector">Configure field
          <select value={field?.key || ''} disabled={saving} onChange={event => {
            setSelected(event.target.value)
            setResult(null)
          }}>
            <option value="">New field</option>
            {fields.map(item => <option key={item.key} value={item.key}>{item.label}</option>)}
          </select>
        </label>
        <DefinitionForm key={JSON.stringify(field || null)} field={field} saving={saving || resource.pending}
          onSave={definition => save(field ? fields.map(item => item.key === field.key ? definition : item) : [...fields, definition],
            definition.key)}
          onRemove={() => save(fields.filter(item => item.key !== field.key), '')} />
      </>}
      {result && <p role={result.error ? 'alert' : 'status'}>{result.message}</p>}
    </section>
  )
}
