import React, { useState } from 'react'
import { request, useResource } from './data.js'
import { ResourceStatus } from './shared.jsx'

const permissionPath = '/admin/project-creation'

export default function ProjectSettings({ revision, onProjectCreated }) {
  const permissions = useResource(permissionPath, revision)
  const [busy, setBusy] = useState(false)
  const [permissionResult, setPermissionResult] = useState(null)
  const [projectResult, setProjectResult] = useState(null)
  const canManage = permissions.data?.can_manage_permissions === true
  const canCreate = permissions.data?.can_create_project === true
  const disabled = busy || permissions.pending

  async function changePermission(person, allowed) {
    setBusy(true)
    setPermissionResult(null)
    try {
      await request(permissionPath, { method: 'PUT', body: { principal: person.id, allowed } })
      setPermissionResult({ message: allowed
        ? `${person.name} can now create projects.`
        : `Removed project creation permission for ${person.name}.` })
    } catch (error) {
      setPermissionResult({ error: true, message: error.message })
    } finally {
      permissions.retry()
      setBusy(false)
    }
  }

  async function createProject(event) {
    event.preventDefault()
    if (disabled) return
    const form = event.currentTarget
    const fields = new FormData(form)
    const prefix = fields.get('prefix').trim().toUpperCase()
    const title = fields.get('title').trim()
    setBusy(true)
    setProjectResult(null)
    try {
      const { project } = await request('/projects', { body: { prefix, title } })
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
  if (!permissions.error && !canManage && !canCreate) return null

  return (
    <section className="panel settings-panel" aria-labelledby="project-settings">
      <h2 id="project-settings">Projects</h2>
      <ResourceStatus resource={permissions} label="project permissions" />
      {!permissions.error && canManage && (
        <fieldset className="project-creation-permissions" disabled={disabled}>
          <legend>Can create projects</legend>
          <ul>
            {permissions.data.principals.map(person => {
              const kind = person.kind === 'human' ? 'Person' : 'Agent'
              return (
                <li key={person.id}>
                  <label>
                    <input type="checkbox" checked={person.can_create_project} disabled={person.managed === false}
                      aria-label={`Allow ${person.name} (${kind}) to create projects`}
                      onChange={event => changePermission(person, event.currentTarget.checked)} />
                    <span>{person.name} <small>{kind}{person.managed === false && ' · Fixed access'}</small></span>
                  </label>
                </li>
              )
            })}
          </ul>
        </fieldset>
      )}
      {permissionResult && <p role={permissionResult.error ? 'alert' : 'status'}>{permissionResult.message}</p>}
      {!permissions.error && canCreate && (
        <form className="ownership-form project-create-form" onSubmit={createProject}>
          <h3>Create project</h3>
          <label>Project name <input name="title" required disabled={disabled} /></label>
          <label>Project code <input name="prefix" required disabled={disabled} autoCapitalize="characters"
            pattern="[A-Za-z][A-Za-z0-9]{0,7}" maxLength={8} title="One to eight letters or numbers, starting with a letter." /></label>
          <button type="submit" disabled={disabled}>Create project</button>
        </form>
      )}
      {projectResult?.error && <p role="alert">{projectResult.message}</p>}
      {projectResult?.project && <p role="status"><a href={`#project=${encodeURIComponent(projectResult.project.prefix)}`}>
        Open {projectResult.project.title}
      </a></p>}
    </section>
  )
}
