import React, { useState } from 'react'
import { useLoadedPages } from './data.js'
import { LoadMore, ResourceStatus, TaskLink, WorkerAssignment, WorkerRunState, WorkerRunTiming, profileName } from './shared.jsx'
import { ReportTime } from './ReportTime.jsx'
import './dri-workers.css'

function WorkerRuns({ owner, revision, project }) {
  const page = useLoadedPages('/reports/workers', { owner, project, order: 'execution' }, revision)
  return (
    <div className="dri-runs-panel">
      <p className="dri-runs-note">Worker status is recorded in the task ledger.</p>
      <ResourceStatus resource={page} label="worker runs" />
      {page.data?.items.length === 0 && <p className="section-message">No recorded runs.</p>}
      <ul className="dri-run-list" role="list">
        {page.data?.items.map(run => (
          <li key={run.id}>
            <div className="dri-run-meta">
              <strong>{profileName(run.worker, run.worker_profile)}</strong>
              <WorkerRunState run={run} />
              {run.updated_at && <span>Last activity <ReportTime at={run.updated_at} /></span>}
            </div>
            <TaskLink reference={run.task_key} id={run.task_id} title={run.task_title} />
            <WorkerAssignment run={run} />
            <WorkerRunTiming run={run} />
          </li>
        ))}
      </ul>
      <LoadMore page={page} label="worker runs" />
    </div>
  )
}

export default function DriWorkers({ owner, counts, revision, project }) {
  const [open, setOpen] = useState(false)
  if (!counts?.recorded) return null
  const running = counts.running + counts.running_reviews
  const started = counts.launched + counts.launched_reviews
  return (
    <details className="dri-runs" onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>Recorded worker runs{Number.isFinite(running) && <> · {running} running · {started} started</>}</summary>
      {open && <WorkerRuns owner={owner} revision={revision} project={project} />}
    </details>
  )
}
