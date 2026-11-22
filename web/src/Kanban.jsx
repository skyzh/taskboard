import React, { useEffect, useState } from 'react'
import { useLoadedPages } from './data.js'
import { LoadMore, ResourceStatus, states } from './shared.jsx'
import { TaskRows } from './WorkRow.jsx'
import './kanban.css'

const columns = ['ready', 'in_progress', 'human_review', 'waiting', 'parked', 'done']
const phoneQuery = '(max-width: 48rem)'

function usePhoneLayout() {
  const [phone, setPhone] = useState(() => window.matchMedia(phoneQuery).matches)
  useEffect(() => {
    const media = window.matchMedia(phoneQuery)
    const synchronize = () => setPhone(media.matches)
    synchronize()
    media.addEventListener('change', synchronize)
    return () => media.removeEventListener('change', synchronize)
  }, [])
  return phone
}

function KanbanColumn({ state, count, filters, project, revision, navigation, onPage }) {
  const page = useLoadedPages('/tasks', {
    project, state, dri: filters.dri, unassigned: filters.unassigned, waiting_on: filters.waiting_on, tag: filters.tag,
    order: state === 'ready' ? 'priority' : undefined,
  }, revision, { rememberedNavigation: navigation })
  const items = page.data?.items
  useEffect(() => {
    if (items) onPage(state, { project, items })
  }, [items, onPage, project, state])

  const title = states[state]
  const label = `tasks marked “${title}”`
  return (
    <section className="kanban-column" aria-labelledby={'kanban-' + state}>
      <h3 id={'kanban-' + state}>{title}{count != null && <span className="kanban-count"> ({count})</span>}</h3>
      <ResourceStatus resource={page} label={label} />
      {items?.length === 0 && <p className="section-message" role="status">No matching tasks.</p>}
      <TaskRows items={items} label={label} />
      <LoadMore page={page} label={label} />
    </section>
  )
}

export default function Kanban({ filters, project, revision, closedAvailable, phoneColumn, setPhoneColumn, counts, navigation, onPage }) {
  const phone = usePhoneLayout()
  const availableColumns = closedAvailable ? [...columns, 'closed'] : columns
  const chosenState = filters.state || (phone ? phoneColumn : null)
  return (
    <>
      {phone && !filters.state && (
        <label className="kanban-column-picker">
          Kanban column
          <select value={phoneColumn} onChange={event => setPhoneColumn(event.target.value)}>
            {availableColumns.map(state => <option key={state} value={state}>{states[state]}</option>)}
          </select>
        </label>
      )}
      <div className={chosenState ? 'kanban-columns' : 'kanban-columns multiple'}
        role={chosenState ? undefined : 'region'} aria-label={chosenState ? undefined : 'Kanban columns'}
        tabIndex={chosenState ? undefined : 0}>
        {(chosenState ? [chosenState] : availableColumns).map(state => (
          <KanbanColumn key={state} state={state} count={phone && chosenState ? counts?.[state] : undefined}
            filters={filters} project={project}
            revision={revision} navigation={navigation} onPage={onPage} />
        ))}
      </div>
    </>
  )
}
