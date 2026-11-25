import React, { useId, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { request } from './data.js'
import { FieldHelp, fieldGuidancePath, useFieldGuidance } from './FieldGuidance.jsx'
import { ResourceStatus } from './shared.jsx'
import TicketFieldSettings from './TicketFieldSettings.jsx'

const fieldLabels = { title: 'Title', brief: 'Brief', next_step: 'Next step', activity: 'Activity' }
const sourceLabels = { project: 'Project override', board: 'Board guidance', default: 'Built-in guidance' }

function GuidanceEditor({ project, field, entry, reload }) {
  const helpId = useId()
  const [draft, setDraft] = useState(null)
  const [saving, setSaving] = useState(false)
  const [result, setResult] = useState(null)
  const text = draft ?? entry.guidance
  const canSave = !saving && text.trim() && text.trim() !== entry.guidance

  async function save(guidance) {
    setSaving(true)
    setResult(null)
    try {
      await request(fieldGuidancePath(project), { method: 'PUT', body: { field, guidance } })
      setDraft(null)
      reload()
      setResult({ message: guidance === null ? 'Inherited guidance restored.' : 'Guidance saved.' })
    } catch (error) {
      setResult({ error: true, message: error.message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="ownership-form" data-reload-blocked={Boolean(saving || draft != null)} onSubmit={event => {
      event.preventDefault()
      if (canSave) save(text.trim())
    }}>
      <label>
        Guidance
        <textarea rows={3} value={text} disabled={saving} aria-describedby={helpId}
          onChange={event => { setDraft(event.target.value); setResult(null) }} />
      </label>
      <FieldHelp field={{ ...entry, guidance: null }} id={helpId} />
      <div className="field-guidance-actions">
        <button type="submit" disabled={!canSave}>Save guidance</button>
        {entry.source === 'project' && <button type="button" disabled={saving} onClick={() => save(null)}>Use inherited guidance</button>}
      </div>
      {result && <p role={result.error ? 'alert' : 'status'}>{result.message}</p>}
    </form>
  )
}

export default function FieldGuidanceSettings({ project, projects, revision }) {
  const [selectedProject, setSelectedProject] = useState(null)
  const [field, setField] = useState('title')
  const scope = projects?.some(item => item.prefix === selectedProject) ? selectedProject : project
  const guidance = useFieldGuidance(scope, revision)
  const entry = guidance.data?.fields?.[field]
  if (!scope || guidance.error?.status === 404) return null

  return (
    <section className="panel settings-panel" aria-labelledby="writing-guidance-heading">
      <h2 id="writing-guidance-heading">Writing guidance</h2>
      {projects?.length > 1 && <label className="settings-preference field-guidance-selector">
        Project for writing guidance
        <span className="embedded-picker">
          <select value={scope} onChange={event => setSelectedProject(event.target.value)}>
            {projects.map(item => <option key={item.prefix} value={item.prefix}>{item.title}</option>)}
          </select>
          <ChevronDown size={14} aria-hidden="true" focusable="false" />
        </span>
      </label>}
      <ResourceStatus resource={guidance} label="writing guidance" />
      {entry && <>
        <label className="settings-preference field-guidance-selector">
          Field
          <span className="embedded-picker">
            <select value={field} onChange={event => setField(event.target.value)}>
              {Object.entries(fieldLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select>
            <ChevronDown size={14} aria-hidden="true" focusable="false" />
          </span>
        </label>
        <p className="report-note">{sourceLabels[entry.source]}</p>
        {guidance.data.editable
          ? <GuidanceEditor key={`${scope}:${field}`} project={scope} field={field} entry={entry} reload={guidance.retry} />
          : <FieldHelp field={entry} />}
        {guidance.data.editable && <TicketFieldSettings key={scope} project={scope} revision={revision} />}
      </>}
    </section>
  )
}
