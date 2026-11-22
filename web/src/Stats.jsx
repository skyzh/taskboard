import React from 'react'
import { queryPath, useResource } from './data.js'
import { AssigneeAvatar, ResourceStatus, Status, TaskLink, Timestamp, eventHref, taskState } from './shared.jsx'
import { useTimeZone } from './timeZone.js'
import './stats.css'

const number = new Intl.NumberFormat('en-US')
const hours = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 })
const sampleMean = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 })

const FLOW_METRICS = [['new', 'New tickets'], ['done', 'Recorded Done'], ['closed', 'Closed']]
const PEER_TIMINGS = [
  ['add_to_start', 'Queue · Add to Start', 'requests that could queue'],
  ['start_to_result', 'Handling · Start to result', 'submitted results'],
  ['add_to_result', 'Submission · Add to result', 'submitted results'],
  ['result_to_ack', 'Acknowledgment · Result to Done', 'acknowledgments'],
]

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
      <p className="stats-note">Each peak counts overlapping recorded intervals within a {span} bucket.</p>
      <p className="stats-legend"><span>Recorded DRI intervals</span><span>Workers with closed runs</span></p>
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
      <p className="stats-note">A task owner’s interval opens at a recorded Start or Resume.</p>
    </section>
  )
}

function elapsed(seconds) {
  if (seconds == null) return 'Unavailable'
  if (seconds < 60) return sampleMean.format(seconds) + ' s'
  if (seconds < 3600) return sampleMean.format(seconds / 60) + ' min'
  return sampleMean.format(seconds / 3600) + ' hr'
}

function TimingCoverage({ timing, population }) {
  return (
    <span className="stats-coverage">
      {number.format(timing.measured)} of {number.format(timing.eligible)} {population} measured
      {timing.unknown > 0 && <span> · {number.format(timing.unknown)} missing or invalid {timing.unknown === 1 ? 'interval' : 'intervals'}</span>}
    </span>
  )
}

function TimingExamples({ examples }) {
  return examples.length > 0 && (
    <details className="stats-bucket-list">
      <summary>View source examples</summary>
      <ul className="stats-evidence">
        {examples.map(({ task, start_event: start, end_event: end }) => (
          <li key={end}>{task} · <a href={eventHref(start)} aria-label={`Open ${task} first event`}>First event</a>
            {' · '}<a href={eventHref(end)} aria-label={`Open ${task} last event`}>Last event</a></li>
        ))}
      </ul>
    </details>
  )
}

function FlowTrend({ buckets }) {
  const { formatTimestamp } = useTimeZone()
  const maximum = Math.max(1, ...buckets.flatMap(row => FLOW_METRICS.map(([key]) => row[key])))
  return (
    <>
      <p className="stats-legend stats-flow-legend">{FLOW_METRICS.map(([key, label]) => <span key={key}>{label}</span>)}</p>
      <div className="stats-chart stats-flow-chart" aria-hidden="true">
        {buckets.map(row => (
          <div key={row.from} title={formatTimestamp(row.from)}>
            {FLOW_METRICS.map(([key, label]) => (
              <span key={key} title={`${label}: ${number.format(row[key])}`} style={{ height: `${100 * row[key] / maximum}%` }} />
            ))}
          </div>
        ))}
      </div>
      <div className="stats-axis" aria-hidden="true">
        <span><Timestamp at={buckets[0].from} /></span><span><Timestamp at={buckets.at(-1).to} /></span>
      </div>
      <details className="stats-bucket-list">
        <summary>View recorded trend counts</summary>
        <table className="stats-table">
          <thead><tr>
            <th scope="col">From</th>
            {FLOW_METRICS.map(([key, label]) => <th key={key} scope="col">{label}</th>)}
          </tr></thead>
          <tbody>
            {buckets.map(row => (
              <tr key={row.from}>
                <th scope="row"><Timestamp at={row.from} /></th>
                {FLOW_METRICS.map(([key]) => <td key={key}>{number.format(row[key])}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </>
  )
}

function TicketFlow({ data }) {
  const flow = data.ticket_flow
  const timing = flow.start_to_done
  return (
    <section className="panel" aria-labelledby="stats-ticket-flow">
      <h2 id="stats-ticket-flow">Ticket turnaround</h2>
      <p className="stats-note"><Timestamp at={data.from} /> to <Timestamp at={data.to} /></p>
      <dl className="stats-totals">
        {FLOW_METRICS.map(([key, label]) => (
          <div key={key}><dt>{label}</dt><dd>{number.format(flow[key])}</dd></div>
        ))}
        <div><dt>Median Start to Done</dt><dd>{elapsed(timing.median_seconds)}</dd></div>
      </dl>
      <TimingCoverage timing={timing} population="Done records" />
      <TimingExamples examples={timing.examples} />
      <p className="stats-note">Closed counts administrative closures. Done includes entries recorded after the work happened. Start to Done measures elapsed time between recorded events.</p>
      <FlowTrend buckets={data.buckets} />
    </section>
  )
}

function PeerRequests({ requests }) {
  return (
    <section className="panel" aria-labelledby="stats-peer-requests">
      <h2 id="stats-peer-requests">Peer requests</h2>
      <dl className="stats-totals">
        {[
          ['opened', 'Opened in this period'], ['pending', 'Awaiting a result'], ['awaiting_ack', 'Awaiting acknowledgment'],
          ['acknowledged', 'Acknowledged'], ['canceled', 'Canceled'],
        ].map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{number.format(requests[key])}</dd></div>)}
      </dl>
      {requests.already_started > 0 && (
        <p className="stats-note">Already started when opened · {number.format(requests.already_started)}</p>
      )}
      {requests.opened > 0 && (
        <table className="stats-table stats-peer-table">
          <thead><tr><th scope="col">Recorded stage</th><th scope="col">Median elapsed</th></tr></thead>
          <tbody>
            {PEER_TIMINGS.map(([key, label, population]) => {
              const timing = requests.timings[key]
              return (
                <tr key={key}>
                  <th scope="row">{label}<TimingCoverage timing={timing} population={population} />
                    <TimingExamples examples={timing.examples} /></th>
                  <td>{elapsed(timing.median_seconds)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
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

function IntegrationActivity({ integrations }) {
  const { github, raft_messages: messages, raft_notices: notices } = integrations
  const githubStatus = { disabled: 'Off', manual: 'Manual only' }[github.status] || 'Unavailable'
  const noticeCount = notices.count == null ? (notices.status === 'sign_in' ? 'Sign in' : 'Unavailable') : number.format(notices.count)
  return (
    <section className="panel" aria-labelledby="stats-integrations">
      <h2 id="stats-integrations">Integration activity</h2>
      <dl className="stats-totals">
        <div><dt>GitHub activity recorded</dt><dd>{github.count == null ? githubStatus : number.format(github.count)}</dd></div>
        <div><dt>Raft messages sent</dt><dd>{messages.count == null ? 'Unavailable' : number.format(messages.count)}</dd></div>
        <div><dt>Confirmed Raft notices</dt><dd>{noticeCount}</dd></div>
      </dl>
      <p className="stats-note">GitHub covers activity recorded from linked issues and pull requests. Raft notices cover confirmed Ready and merge sends you can see.</p>
      {github.status === 'disabled' && github.count != null && <p className="stats-note">GitHub activity collection is off. The count uses saved records.</p>}
      {github.status === 'manual' && github.count != null && <p className="stats-note">GitHub activity can only be imported manually.</p>}
      {github.older_records && <p className="stats-note">Some older GitHub records lack an event ID and are excluded.</p>}
    </section>
  )
}

export default function Stats({ revision, period = 'day', project, projects }) {
  const resource = useResource(queryPath('/stats', { period, project }), revision)
  const data = resource.data
  function changeScope(nextProject, nextPeriod) {
    const filters = new URLSearchParams({
      ...(nextProject && { project: nextProject }), ...(nextPeriod !== 'day' && { period: nextPeriod }),
    })
    window.location.hash = filters.size ? `stats?${filters}` : 'stats'
  }
  return (
    <div className="stats-page">
      <div className="page-heading">
        <h1 id="stats">Stats</h1>
        <div className="stats-controls">
          <label>Project <select value={project || ''} onChange={event => changeScope(event.target.value, period)}>
            <option value="">All projects</option>
            {project && !projects?.some(entry => entry.prefix === project) && <option value={project}>{project}</option>}
            {projects?.map(entry => <option key={entry.prefix} value={entry.prefix}>{entry.title}</option>)}
          </select></label>
          <label>Time period <select value={period} onChange={event => changeScope(project, event.target.value)}>
            <option value="hour">Past hour</option>
            <option value="day">Past day</option>
            <option value="week">Past week</option>
          </select></label>
        </div>
      </div>
      <ResourceStatus resource={resource} label="stats" />
      {data && (
        <>
          {data.ticket_flow && <TicketFlow data={data} />}
          {data.peer_requests && <PeerRequests requests={data.peer_requests} />}
          {data.integrations && <IntegrationActivity integrations={data.integrations} />}
          <ActiveChart buckets={data.buckets} width={data.bucket_seconds} />
          <section className="panel" aria-labelledby="stats-recorded-work">
            <h2 id="stats-recorded-work">Recorded activity and worker time</h2>
            {!data.ticket_flow && <p className="stats-note"><Timestamp at={data.from} /> to <Timestamp at={data.to} /></p>}
            <dl className="stats-totals">
              <div><dt>Activity events</dt><dd>{number.format(data.activity.events)}</dd></div>
              {!data.ticket_flow && <div><dt>Recorded Done</dt><dd>{number.format(data.activity.completions)}</dd></div>}
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
          <Breakdown activity={data.activity} />
        </>
      )}
    </div>
  )
}
