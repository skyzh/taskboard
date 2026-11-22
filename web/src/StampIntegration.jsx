import React, { useState } from 'react'
import { request, useResource } from './data.js'
import { ReportTime } from './ReportTime.jsx'

const labels = {
  unconfigured: 'Unconfigured',
  unchecked: 'Awaiting first check',
  failing: 'Last check failed',
  verified: 'Last check passed',
}

export default function StampIntegration({ project, revision }) {
  const path = project ? `/projects/${encodeURIComponent(project)}/stamp-reviews` : null
  const { data, retry } = useResource(path, revision)
  const [pull, setPull] = useState(null)
  const [pending, setPending] = useState(false)
  const [result, setResult] = useState(null)
  if (!data) return null

  const value = pull ?? data.pull ?? ''
  async function check(event) {
    event.preventDefault()
    setPending(true)
    setResult(null)
    try {
      const { result: outcome } = await request(path, { body: { pull: value.trim() } })
      setResult({ message: `Recorded ${outcome.recorded} approvals. Returned ${outcome.ready_for_acknowledgment} review requests.` })
    } catch (error) {
      setResult({ error: error.message })
    } finally {
      setPending(false)
      retry()
    }
  }

  return (
    <details className="stamp-integration">
      <summary>Stamp review integration · {labels[data.state]}</summary>
      {data.repository && <p>Repository <strong>{data.repository}</strong></p>}
      {data.checked_at && <p>Last check <ReportTime at={data.checked_at} /></p>}
      {data.state === 'failing' && data.succeeded_at && <p>Last success <ReportTime at={data.succeeded_at} /></p>}
      {data.state === 'unconfigured' && <p>Ask the board operator to configure the project repository and a repository-scoped Stamp reader.</p>}
      {data.failure === 'reader_unavailable' && <p>Ask the board operator to install the Stamp CLI and check the service PATH.</p>}
      {data.failure === 'read_failed' && <p>Ask the board operator to verify Stamp read access to the repository and check provider availability.</p>}
      {data.repository && (
        <form onSubmit={check}>
          <label>
            Linked GitHub pull request
            <input type="url" required placeholder={`https://github.com/${data.repository}/pull/123`}
              value={value} onChange={event => setPull(event.target.value)} />
          </label>
          <button disabled={pending || !value.trim()}>{pending ? 'Checking Stamp…' : 'Refresh Stamp reviews'}</button>
        </form>
      )}
      {result?.message && <p role="status">{result.message}</p>}
      {result?.error && <p role="alert">{result.error}</p>}
      <a href="https://github.com/skyzh/taskboard/blob/main/docs/taskboard/GITHUB-EXTENSIONS.md#stamp-setup-and-recovery" rel="noreferrer">
        Stamp setup and recovery guide
      </a>
    </details>
  )
}
