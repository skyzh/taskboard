import React from 'react'
import { queryPath, useResource } from './data.js'
import { Markdown } from './Markdown.jsx'

export const agentPromptLabels = {
  checkpoint: 'Checkpoint',
  'end-task': 'End task',
  knowledge: 'Knowledge',
  'inbox-follow-up': 'Inbox follow-up',
  'goal-review': 'Goal review',
  'goal-explore': 'Goal exploration',
  'ready-pickup': 'Ready pickup',
  'stalled-follow-up': 'Stalled follow-up',
  'review-handoff': 'Review handoff',
  'worker-handoff': 'Worker handoff',
  'next-action': 'Suggested next action',
}

export function WorkflowPrompt({ entry }) {
  if (!entry?.prompt) return null
  return <>
    <h3>{agentPromptLabels[entry.key]}</h3>
    <Markdown text={entry.prompt} />
  </>
}

export function fieldGuidancePath(project) {
  return `/projects/${encodeURIComponent(project)}/field-guidance`
}

export function useFieldGuidance(project, revision, agent) {
  return useResource(project ? queryPath(fieldGuidancePath(project), { agent }) : null, revision)
}

export function FieldHelp({ field, id }) {
  if (!field) return null
  return (
    <div className="field-guidance-help" id={id}>
      {field.guidance && <p>{field.guidance}</p>}
      {field.where && <p>{field.where}</p>}
      {field.format && <p><strong>Format</strong> {field.format}</p>}
      {field.example && <p><strong>Example</strong> {field.example}</p>}
    </div>
  )
}
