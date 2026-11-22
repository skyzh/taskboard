import React from 'react'
import { queryPath, useResource } from './data.js'
import { AssigneeAvatar, ResourceStatus, Status, TaskLink, Timestamp, taskState } from './shared.jsx'
import { useTimeZone } from './timeZone.js'
import './stats.css'

const number = new Intl.NumberFormat('en-US')
const hours = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 })
const sampleMean = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 })

const WATCHER_METRICS = [
  ['ready_at_first_idle', 'Ready at first idle sample'],
  ['idle_to_wake_seconds', 'First idle to wake (s)'],
  ['wake_to_start_seconds', 'Wake to recorded Start (s)'],
  ['working_seconds', 'Sampled working time (s)'],
  ['advanced_tasks', 'Tasks advanced'],
  ['completed_tasks', 'Tasks completed'],
]

function WatcherStats({ cohorts }) {
  return (
    <section className="panel" aria-labelledby="stats-watcher">
      <h2 id="stats-watcher">Ready watcher pilot</h2>
      <p className="stats-note">Pilot cohorts use their recorded observation windows.</p>
      <table className="stats-table">
        <thead><tr>
          <th scope="col">Measure</th>
          {cohorts.map(cohort => <th key={cohort.key} scope="col">{cohort.label}</th>)}
        </tr></thead>
        <tbody>
          <tr>
            <th scope="row">Observed runs</th>
            {cohorts.map(cohort => <td key={cohort.key}>{number.format(cohort.run_count)}</td>)}
          </tr>
          <tr>
            <th scope="row">Runs with a Ready count</th>
            {cohorts.map(cohort => {
              const measured = cohort.metrics.ready_at_first_idle?.measured_runs
              return <td key={cohort.key}>{measured == null ? null : number.format(measured)}</td>
            })}
          </tr>
          {WATCHER_METRICS.map(([key, label]) => (
            <tr key={key}>
              <th scope="row">{label}</th>
              {cohorts.map(cohort => {
                const measurement = cohort.metrics[key]
                return (
                  <td key={cohort.key}>
                    {measurement?.mean != null && (
                      <>
                        {sampleMean.format(measurement.mean)}
                        <div className="stats-note">{number.format(measurement.measured_runs)} measured</div>
                      </>
                    )}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="stats-note">Metric values are averages. Blank cells indicate missing observations.</p>
    </section>
  )
}

function ActiveChart({ buckets, width }) {
  const { formatTimestamp } = useTimeZone()
  const maximum = Math.max(1, ...buckets.flatMap(row => [row.dris, row.workers]))
  const span = width < 3600 ? `${width / 60}-minute` : `${width / 3600}-hour`
  return (
    <section className="panel" aria-labelledby="stats-chart">
      <h2 id="stats-chart">Peak recorded DRIs and workers</h2>
      <p className="stats-note">Each count shows the highest number active at once in a {span} interval.</p>
      <p className="stats-legend"><span>Active DRIs</span><span>Workers with closed runs</span></p>
      <div className="stats-chart" aria-hidden="true">
        {buckets.map(row => (
          <div key={row.from} title={`${formatTimestamp(row.from)} · ${row.dris} ${row.dris === 1 ? 'DRI' : 'DRIs'}, ${row.workers} ${row.workers === 1 ? 'worker' : 'workers'}`}>
            <span style={{ height: `${100 * row.dris / maximum}%` }} />
            <span style={{ height: `${100 * row.workers / maximum}%` }} />
          </div>
        ))}
      </div>
      <div className="stats-axis" aria-hidden="true">
        <span><Timestamp at={buckets[0].from} /></span><span><Timestamp at={buckets.at(-1).to} /></span>
      </div>
      <details className="stats-bucket-list">
        <summary>View interval counts</summary>
        <table className="stats-table">
          <thead><tr><th scope="col">From</th><th scope="col">Peak DRIs</th><th scope="col">Peak workers</th></tr></thead>
          <tbody>
            {buckets.map(row => (
              <tr key={row.from}><th scope="row"><Timestamp at={row.from} /></th><td>{row.dris}</td><td>{row.workers}</td></tr>
            ))}
          </tbody>
        </table>
      </details>
      <p className="stats-note">Task owners (DRIs) count as active after a recorded start or resume.</p>
    </section>
  )
}

function Breakdown({ activity }) {
  const { tasks, task_count: taskCount, agents, agent_count: agentCount, other_events: otherEvents } = activity
  return (
    <div className="stats-breakdowns">
      <section className="panel" aria-labelledby="stats-tasks">
        <h2 id="stats-tasks">By task</h2>
        {tasks.length ? (
          <table className="stats-table stats-task-table">
            <thead><tr><th scope="col">Task</th><th scope="col">Events</th><th scope="col">Status</th></tr></thead>
            <tbody>
              {tasks.map(row => (
                <tr key={row.id}>
                  <th scope="row"><TaskLink id={row.id} reference={row.key} title={row.title} /></th>
                  <td>{row.events}</td>
                  <td>{row.state ? <Status state={taskState(row)} /> : 'Unavailable'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="stats-note">No task activity in this period.</p>}
        {taskCount > tasks.length && <p className="stats-note">Showing the {tasks.length} most active of {taskCount} tasks.</p>}
      </section>
      <section className="panel" aria-labelledby="stats-agents">
        <h2 id="stats-agents">By verified agent</h2>
        {agents.length ? (
          <table className="stats-table">
            <thead><tr><th scope="col">Agent</th><th scope="col">Events</th><th scope="col">Completed</th></tr></thead>
            <tbody>
              {agents.map((row, index) => (
                <tr key={index}>
                  <th scope="row">
                    <span className="assignee stats-agent">
                      <AssigneeAvatar name={row.name} url={row.avatar_url} />
                      <span>{row.name}</span>
                    </span>
                  </th>
                  <td>{row.events}</td><td>{row.completions}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="stats-note">No events attributed to verified agents.</p>}
        {agentCount > agents.length && <p className="stats-note">Showing the {agents.length} most active of {agentCount} agents.</p>}
        {otherEvents > 0 && <p className="stats-note">{number.format(otherEvents)} {otherEvents === 1 ? 'event came' : 'events came'} from other actors or sources.</p>}
      </section>
    </div>
  )
}

export default function Stats({ revision, period = 'day', project }) {
  const resource = useResource(queryPath('/stats', { period }), revision)
  const data = resource.data
  return (
    <div className="stats-page">
      <div className="page-heading">
        <h1 id="stats">Stats</h1>
        <div className="stats-controls">
          <label>Time period <select value={period} onChange={event => {
            const selection = event.target.value
            const filters = new URLSearchParams({
              ...(project && { project }), ...(selection !== 'day' && { period: selection }),
            })
            window.location.hash = filters.size ? `stats?${filters}` : 'stats'
          }}>
            <option value="hour">Past hour</option>
            <option value="day">Past day</option>
            <option value="week">Past week</option>
          </select></label>
        </div>
      </div>
      <ResourceStatus resource={resource} label="stats" />
      {data && (
        <>
          <section className="panel" aria-label="Period totals">
            <p className="stats-note"><Timestamp at={data.from} /> to <Timestamp at={data.to} /></p>
            <dl className="stats-totals">
              <div><dt>Activity events</dt><dd>{number.format(data.activity.events)}</dd></div>
              <div><dt>Tasks completed</dt><dd>{number.format(data.activity.completions)}</dd></div>
              <div><dt>Recorded hours on closed worker runs</dt><dd>{hours.format(data.worker_hours.elapsed_seconds / 3600)}</dd></div>
            </dl>
            <p className="stats-note">
              Measured runs {number.format(data.worker_hours.measured_runs)}
              {' · '}Runs still open at period end {number.format(data.worker_hours.unclosed_runs)}
              {' · '}Invalid runs {number.format(data.worker_hours.invalid_runs)}
            </p>
            <p className="stats-note">Overlapping runs each count toward the total.</p>
          </section>
          {data.watcher_measurements?.cohorts.length > 0 && (
            <WatcherStats cohorts={data.watcher_measurements.cohorts} />
          )}
          <ActiveChart buckets={data.buckets} width={data.bucket_seconds} />
          <Breakdown activity={data.activity} />
        </>
      )}
    </div>
  )
}
