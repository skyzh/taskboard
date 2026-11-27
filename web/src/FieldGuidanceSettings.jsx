import React, { useId, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { request, useLoadedPages } from './data.js'
import { FieldHelp, fieldGuidancePath, useFieldGuidance } from './FieldGuidance.jsx'
import { LoadMore, ResourceStatus, profileName } from './shared.jsx'
import TicketFieldSettings from './TicketFieldSettings.jsx'

const fieldLabels = { title: 'Title', brief: 'Brief', next_step: 'Next step', activity: 'Activity' }
const promptLabels = { checkpoint: 'Checkpoint', 'end-task': 'End task', knowledge: 'Knowledge', 'inbox-follow-up': 'Inbox follow-up' }
const sourceLabels = { agent: 'Agent override', project: 'Project override', board: 'Board guidance', default: 'Built-in guidance' }

function GuidanceEditor({ project, field, entry, reload, prompt = false, agent }) {
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
      const body = prompt ? { prompt: field, guidance, ...(agent && { agent }) } : { field, guidance }
      await request(fieldGuidancePath(project), { method: 'PUT', body })
      setDraft(null)
      reload()
      const label = prompt ? 'Prompt' : 'Guidance'
      setResult({ message: guidance === null ? `Inherited ${label.toLowerCase()} restored.` : `${label} saved.` })
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
        {prompt ? `${promptLabels[field]} prompt` : 'Guidance'}
        <textarea rows={prompt ? 9 : 3} maxLength={prompt ? 8000 : undefined} value={text} disabled={saving}
          aria-describedby={prompt ? undefined : helpId}
          onChange={event => { setDraft(event.target.value); setResult(null) }} />
      </label>
      {!prompt && <FieldHelp field={{ ...entry, guidance: null }} id={helpId} />}
      <div className="field-guidance-actions">
        <button type="submit" disabled={!canSave}>{prompt ? 'Save prompt' : 'Save guidance'}</button>
        {entry.source === (agent ? 'agent' : 'project') && <button type="button" disabled={saving} onClick={() => save(null)}>
          {prompt ? 'Use inherited prompt' : 'Use inherited guidance'}
        </button>}
      </div>
      {result && <p role={result.error ? 'alert' : 'status'}>{result.message}</p>}
    </form>
  )
}

function GuidancePicker({ label, value, choices, onChange }) {
  return (
    <label className="settings-preference field-guidance-selector">
      {label}
      <span className="embedded-picker">
        <select value={value} onChange={event => onChange(event.target.value)}>
          {choices.map(([key, title]) => <option key={key} value={key}>{title}</option>)}
        </select>
        <ChevronDown size={14} aria-hidden="true" focusable="false" />
      </span>
    </label>
  )
}

export function AgentPromptSettings({ project, projects, revision, initialPrompt }) {
  const [selectedProject, setSelectedProject] = useState(null)
  const [prompt, setPrompt] = useState(Object.hasOwn(promptLabels, initialPrompt) ? initialPrompt : 'checkpoint')
  const [agentSelection, setAgentSelection] = useState(null)
  const scope = projects?.some(item => item.prefix === selectedProject) ? selectedProject : project
  const defaults = useFieldGuidance(scope, revision)
  const canTargetAgent = prompt === 'inbox-follow-up' && defaults.data?.editable
  const agent = canTargetAgent && agentSelection?.project === scope ? agentSelection.id : ''
  const assignees = useLoadedPages(canTargetAgent ? '/assignees' : null, { project: scope }, revision)
  const targeted = useFieldGuidance(agent ? scope : null, revision, agent)
  const guidance = agent ? targeted : defaults
  const entry = guidance.data?.prompts?.[prompt]
  const agents = (assignees.data?.items || []).filter(person => person.kind === 'agent')
  if (!scope || defaults.error?.status === 404) return null

  return (
    <section className="panel settings-panel" aria-labelledby="agent-prompts">
      <h2 id="agent-prompts" tabIndex={-1}>Agent prompts</h2>
      {projects?.length > 1 && <GuidancePicker label="Project for agent prompts" value={scope}
        choices={projects.map(item => [item.prefix, item.title])} onChange={setSelectedProject} />}
      <ResourceStatus resource={guidance} label="agent prompts" />
      {defaults.data?.prompts && <GuidancePicker label="Prompt" value={prompt}
        choices={Object.entries(promptLabels).filter(([name]) => defaults.data.prompts[name])} onChange={setPrompt} />}
      {canTargetAgent && <>
        <GuidancePicker label="Applies to" value={agent}
          choices={[['', 'Project default'], ...agents.map(person => [person.id, profileName(person.id, person)])]}
          onChange={id => setAgentSelection({ project: scope, id })} />
        <ResourceStatus resource={assignees} label="project agents" />
        <LoadMore page={assignees} label="project agents" />
      </>}
      {entry && <>
        <p className="report-note">{sourceLabels[entry.source]}</p>
        {guidance.data.editable && entry.source
          ? <GuidanceEditor key={`${scope}:${prompt}:${agent}`} project={scope} field={prompt} prompt agent={agent}
            entry={{ ...entry, guidance: entry.prompt }} reload={guidance.retry} />
          : <FieldHelp field={{ guidance: entry.prompt }} />}
      </>}
    </section>
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
      {projects?.length > 1 && <GuidancePicker label="Project for writing guidance" value={scope}
        choices={projects.map(item => [item.prefix, item.title])} onChange={setSelectedProject} />}
      <ResourceStatus resource={guidance} label="writing guidance" />
      {entry && <>
        <GuidancePicker label="Field" value={field} choices={Object.entries(fieldLabels)} onChange={setField} />
        <p className="report-note">{sourceLabels[entry.source]}</p>
        {guidance.data.editable
          ? <GuidanceEditor key={`${scope}:${field}`} project={scope} field={field} entry={entry} reload={guidance.retry} />
          : <FieldHelp field={entry} />}
        {guidance.data.editable && <TicketFieldSettings key={scope} project={scope} revision={revision} />}
      </>}
    </section>
  )
}
