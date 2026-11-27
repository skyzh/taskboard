import React from 'react'
import { queryPath, useResource } from './data.js'

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
