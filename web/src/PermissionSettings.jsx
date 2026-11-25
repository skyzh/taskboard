import React, { useState } from 'react'
import { queryPath, request, useResource } from './data.js'
import { ResourceStatus } from './shared.jsx'

const kinds = { human: 'Human', team_dri: 'Team DRI', local_dri: 'Local DRI' }
const projectPermissions = {
  view_project: 'View project',
  edit_shared_content: 'Edit tickets and Knowledge',
  edit_project_prompts: 'Edit project prompts',
  manage_project_workflow: 'Manage project workflow',
  manage_project_owners: 'Manage project owners',
}

function Permission({ label, value, edit, busy }) {
  const status = value === undefined ? 'Restricted' : value ? 'Allowed' : 'No access'
  return <div className="permission-row">
    <dt>{label}</dt>
    <dd>{edit ? <label>
      <input type="checkbox" aria-label={label} checked={value} onChange={event => edit(event.target.checked)} disabled={busy} />
      {status}
    </label> : status}</dd>
  </div>
}

export default function PermissionSettings({ actor, project, projects, revision, onProjectChange, onChanged }) {
  const [selected, setSelected] = useState(null)
  const directory = useResource(project ? queryPath('/settings/permissions', { project }) : null, revision)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const items = directory.data && directory.data.project === project ? directory.data.items : []
  const person = items.find(item => item.id === selected) || items.find(item => item.id === actor.id) || items[0]
  const title = projects?.find(item => item.prefix === project)?.title || project
  const names = new Map()
  for (const { kind, name } of items) {
    const key = `${kind}:${name}`
    names.set(key, (names.get(key) ?? 0) + 1)
  }
  function personLabel(item) {
    const duplicate = names.get(`${item.kind}:${item.name}`) > 1
    const disambiguation = item.handle ? `@${item.handle}` : duplicate ? item.id : ''
    return [item.name, kinds[item.kind], disambiguation].filter(Boolean).join(' · ')
  }

  async function change(path, allowed, label) {
    setBusy(true)
    setResult(null)
    try {
      await request(path, { method: 'PUT', body: { principal: person.id, allowed } })
      setResult({ message: `${label} updated for ${person.name}.` })
      onChanged()
    } catch (error) {
      setResult({ error: true, message: error.message })
      directory.retry()
    } finally {
      setBusy(false)
    }
  }

  return <section className="panel settings-panel permission-settings" aria-labelledby="permissions">
    <h2 id="permissions" tabIndex={-1}>Permissions</h2>
    {projects?.length > 1 && <label className="settings-preference">Project
      <select aria-label="Project" value={project} onChange={event => { setResult(null); onProjectChange(event.target.value) }} disabled={busy}>
        {projects.map(item => <option key={item.prefix} value={item.prefix}>{item.title}</option>)}
      </select>
    </label>}
    <ResourceStatus resource={directory} label="permissions" />
    {person && <>
      <label className="settings-preference">Person or agent
        <select aria-label="Person or agent" value={person.id} onChange={event => { setResult(null); setSelected(event.target.value) }} disabled={busy}>
          {items.map(item => <option key={item.id} value={item.id}>{personLabel(item)}</option>)}
        </select>
      </label>
      <h3>Board</h3>
      <dl className="permission-list">
        <Permission label="Create project" value={person.permissions.create_projects}
          edit={person.can_edit_create_projects && (allowed => change('/admin/project-creation', allowed, 'Create project'))}
          busy={busy || directory.pending} />
        <Permission label="Manage board permissions" value={person.permissions.manage_board_permissions} />
      </dl>
      <h3>{title} permissions</h3>
      <dl className="permission-list">
        <Permission label="Project owner" value={person.project_owner}
          edit={person.can_edit_project_owner && (allowed => change(`/projects/${encodeURIComponent(project)}/owners`, allowed, 'Project owner'))}
          busy={busy || directory.pending} />
        {Object.entries(projectPermissions).filter(([key]) => key in person.permissions).map(([key, label]) =>
          <Permission key={key} label={label} value={person.permissions[key]} />)}
      </dl>
    </>}
    {result && <p role={result.error ? 'alert' : 'status'}>{result.message}</p>}
  </section>
}
