import React, { useState } from 'react'
import { request } from './data.js'
import { EvidenceLinks } from './shared.jsx'

const methods = { measured: 'Measured', estimated: 'Estimated', unknown: 'Unknown' }
const hourFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 })

function displayValue(field) {
  if (field.method === 'unknown') return 'Unknown'
  if (field.kind === 'duration') return hourFormat.format(Number(field.value) / 3600) + ' h'
  if (field.kind === 'money') return field.value + ' ' + field.currency
  return field.value
}

function FieldEditor({ field, ticket, onChanged }) {
  const [method, setMethod] = useState(field.method)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState(null)
  const known = method !== 'unknown'
  const url = field.links?.[0]?.url || ''

  async function submit(event) {
    event.preventDefault()
    if (saving) return
    const form = new FormData(event.currentTarget)
    const body = { field: field.key, method, basis: form.get('basis') }
    if (known) {
      body.value = form.get('value')
      body.coverage = form.get('coverage')
      body.url = form.get('url')
      if (field.kind === 'money') body.currency = form.get('currency').toUpperCase()
    }
    setSaving(true)
    setResult(null)
    try {
      await request(`/tasks/${encodeURIComponent(ticket)}/ticket-fields`, { method: 'PUT', body })
      setDirty(false)
      setResult({ message: 'Field saved.' })
      onChanged()
    } catch (error) {
      setResult({ error: true, message: error.message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <details className="task-ownership">
      <summary>Edit {field.label}</summary>
      <form className="ownership-form" onSubmit={submit} onChange={() => setDirty(true)}
        data-reload-blocked={saving || dirty}>
        <p>{field.prompt}</p>
        <label>Method
          <select value={method} onChange={event => setMethod(event.target.value)} disabled={saving}>
            {Object.entries(methods).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        {known && <>
          <label>{field.kind === 'duration' ? 'Whole seconds' : 'Value'}
            <input name="value" defaultValue={field.value || ''} disabled={saving} required
              maxLength={field.kind === 'text' ? 500 : 22} />
          </label>
          {field.kind === 'money' && <label>Currency
            <input name="currency" defaultValue={field.currency || ''} maxLength={3} pattern="[A-Za-z]{3}"
              disabled={saving} required />
          </label>}
          <label>Coverage
            <select name="coverage" defaultValue={field.coverage || 'partial'} disabled={saving}>
              <option value="partial">Partial</option><option value="complete">Complete</option>
            </select>
          </label>
          <label>Ticket evidence URL
            <input name="url" type="url" defaultValue={url} maxLength={2000} disabled={saving} required />
          </label>
        </>}
        <label>Source or estimation basis
          <input name="basis" defaultValue={field.basis || ''} maxLength={240} disabled={saving} required />
        </label>
        <button type="submit" disabled={saving || !dirty}>Save field</button>
        {result && <p role={result.error ? 'alert' : 'status'}>{result.message}</p>}
      </form>
    </details>
  )
}

export default function TicketFields({ configuration, ticket, editing, onChanged }) {
  return configuration?.fields?.map(field => (
    <div key={field.key}>
      <dt>{field.label}</dt>
      <dd>
        <span>
          {displayValue(field)}
          {field.method === 'estimated' && ' · Estimated'}
          {field.coverage === 'partial' && ' · Partial'}
        </span>
        {field.basis && <details>
          <summary>Evidence</summary>
          <p>{field.basis}</p>
          <EvidenceLinks links={field.links} />
        </details>}
        {configuration.editable && <div hidden={!editing}>
          <FieldEditor key={field.event_id || field.kind} field={field} ticket={ticket} onChanged={onChanged} />
        </div>}
      </dd>
    </div>
  ))
}
