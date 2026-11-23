import React from 'react'
import { useResource } from './data.js'

export function fieldGuidancePath(project) {
  return `/projects/${encodeURIComponent(project)}/field-guidance`
}

export function useFieldGuidance(project, revision) {
  return useResource(project ? fieldGuidancePath(project) : null, revision)
}

export function FieldHelp({ field, id }) {
  if (!field) return null
  return (
    <div className="field-guidance-help" id={id}>
      {field.guidance && <p>{field.guidance}</p>}
      <p>{field.where}</p>
      <p><strong>Example</strong> {field.example}</p>
    </div>
  )
}
