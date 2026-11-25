import React, { useEffect, useRef, useState } from 'react'
import { queryPath, useResource } from './data.js'
import { ProfileSignIn } from './Profile.jsx'
import { ResourceStatus } from './shared.jsx'

const kinds = { human: 'Human', team_dri: 'Team DRI', local_dri: 'Local DRI' }
const permissionLabels = {
  create_projects: 'Create projects',
  manage_board_permissions: 'Manage board permissions',
  view_project: 'View project',
  edit_shared_content: 'Edit tickets and Knowledge',
  edit_project_prompts: 'Edit project prompts',
  manage_project_workflow: 'Manage project workflow',
  manage_project_owners: 'Manage project owners',
}

export default function RegistrationDirectory({ available, session, revision, project, projects }) {
  const [selected, setSelected] = useState(null)
  const scope = projects?.some(item => item.prefix === selected) ? selected : project
  const directory = useResource(available ? queryPath('/admin/registrations', { project: scope }) : null, revision)
  const heading = useRef(null)
  useEffect(() => { heading.current?.focus({ preventScroll: true }) }, [])
  const items = directory.data?.items ?? []
  const names = new Map()
  for (const { kind, name } of items) {
    const key = `${kind}:${name}`
    names.set(key, (names.get(key) ?? 0) + 1)
  }

  return <section className="settings" aria-labelledby="manage">
    <div className="page-heading"><h1 id="manage" ref={heading} tabIndex={-1}>Manage</h1></div>
    {session.pending && !session.data ? <ResourceStatus resource={session} label="your session" />
      : !available ? <div className="panel settings-panel">
        <p>The board’s configuration manager can view registered people and agents.</p>
        <ProfileSignIn session={session} fragment="#manage" />
      </div> : <section className="panel settings-panel" aria-labelledby="registered-identities">
        <h2 id="registered-identities">Registered people and agents</h2>
        {projects?.length > 1 && <label className="settings-preference">Project permissions
          <select value={scope || projects[0].prefix} onChange={event => setSelected(event.target.value)}>
            {projects.map(item => <option key={item.prefix} value={item.prefix}>{item.title}</option>)}
          </select>
        </label>}
        <ResourceStatus resource={directory} label="registered people and agents" />
        {!directory.pending && !directory.error && items.length === 0 && <p>No people or agents are registered on this board.</p>}
        {!directory.error && items.length > 0 && <ul className="registration-directory" aria-labelledby="registered-identities" role="list">
          {items.map(({ id, name, kind, handle, permissions }) => <li key={id}>
            <span>{name}</span>
            <small>{kinds[kind] || 'Registered identity'}
              {handle ? ` · @${handle}` : names.get(`${kind}:${name}`) > 1 && <> · <code>{id}</code></>}
            </small>
            {permissions && <small>{Object.entries(permissionLabels)
              .filter(([key]) => permissions[key]).map(([, permission]) => permission).join(' · ') || 'No permissions in this project'}</small>}
          </li>)}
        </ul>}
      </section>}
  </section>
}
