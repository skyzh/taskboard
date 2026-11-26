import React, { useState } from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { request, useResource } from './data.js'
import { ResourceStatus } from './shared.jsx'

const permissionPath = '/admin/project-creation'

export default function ProjectSettings({ revision, onProjectCreated }) {
  const permissions = useResource(permissionPath, revision)
  const [busy, setBusy] = useState(false)
  const [projectResult, setProjectResult] = useState(null)
  const canCreate = permissions.data?.can_create_project === true
  const disabled = busy || permissions.pending

  async function createProject(event) {
    event.preventDefault()
    if (disabled) return
    const form = event.currentTarget
    const fields = new FormData(form)
    const prefix = fields.get('prefix').trim().toUpperCase()
    const title = fields.get('title').trim()
    const visibility = fields.get('visibility')
    setBusy(true)
    setProjectResult(null)
    try {
      const { project } = await request('/projects', {
        body: { prefix, title, ...(visibility === 'ring1' && { visibility }) },
      })
      form.reset()
      setProjectResult({ project })
      onProjectCreated()
    } catch (error) {
      setProjectResult({ error: true, message: error.message })
      if (error.status === 403) permissions.retry()
    } finally {
      setBusy(false)
    }
  }

  if ([401, 403, 404].includes(permissions.error?.status)) return null
  if (!permissions.error && !canCreate) return null

  return (
    <section className="panel settings-panel" aria-labelledby="project-settings">
      <h2 id="project-settings">Projects</h2>
      <ResourceStatus resource={permissions} label="project permissions" />
      {canCreate && (
        <form className="ownership-form project-create-form" onSubmit={createProject}>
          <h3>Create project</h3>
          <label>Project name <input name="title" required disabled={disabled} /></label>
          <label>
            Project code
            <input name="prefix" required disabled={disabled} autoCapitalize="characters"
              pattern="[A-Za-z][A-Za-z0-9]{0,7}" maxLength={8} aria-describedby="project-code-help" />
          </label>
          <p className="report-note" id="project-code-help">Use one to eight letters or numbers, starting with a letter.
            The code appears in ticket IDs, such as OPS-1.</p>
          <label>
            Visibility
            <select name="visibility" defaultValue="shared" disabled={disabled} aria-describedby="project-visibility-help">
              <option value="shared">Shared</option>
              <option value="ring1">Ring 1 (private)</option>
            </select>
          </label>
          <p className="report-note" id="project-visibility-help">
            Only listed people and agents can open a Ring 1 project and its tickets, Knowledge, and activity.
          </p>
          <button type="submit" disabled={disabled}>Create project</button>
        </form>
      )}
      {projectResult?.error && <p role="alert">{projectResult.message}</p>}
      {projectResult?.project && <p role="status">
        <BoardLink href={`#project=${encodeURIComponent(projectResult.project.prefix)}`}>
          Open {projectResult.project.title}
        </BoardLink>
        {projectResult.project.visibility === 'ring1' && <>
          {' · '}
          <BoardLink href={`#settings?section=permissions&project=${encodeURIComponent(projectResult.project.prefix)}`}>
            Manage Ring 1 access
          </BoardLink>
        </>}
      </p>}
    </section>
  )
}
