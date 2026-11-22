import React from 'react'
import { useResource } from './data.js'
import { useTimeZone } from './timeZone.js'

function windowTitle(kind, minutes) {
  if (minutes == null) return kind
  if (minutes % 1440 === 0) return `${minutes / 1440}-day window`
  if (minutes % 60 === 0) return `${minutes / 60}-hour window`
  return `${minutes}-minute window`
}

export function CodexUsageDetails({ usage }) {
  const { formatTimestamp } = useTimeZone()
  return (
    <>
      {usage.sample && <p>Sample data</p>}
      <p>Last read <time dateTime={usage.captured_at}>{formatTimestamp(usage.captured_at)}</time></p>
      <dl className="codex-usage">
        {usage.buckets.map(({ label, primary, secondary }) => (
          <React.Fragment key={label}>
            <dt>{label}</dt>
            <dd>
              {[['Primary window', primary], ['Secondary window', secondary]].map(([kind, window]) => window && (
                <div key={kind}>
                  <strong>{windowTitle(kind, window.duration_minutes)}</strong>
                  <span>{window.remaining_percent == null ? 'Remaining quota unavailable'
                    : `${window.remaining_percent.toLocaleString(undefined, { maximumFractionDigits: 2 })}% remaining`}</span>
                  <span>{window.resets_at == null ? 'Reset time unavailable'
                    : `Resets ${formatTimestamp(window.resets_at * 1000)}`}</span>
                </div>
              ))}
              {!primary && !secondary && 'Quota unavailable'}
            </dd>
          </React.Fragment>
        ))}
        <dt>Paid credits</dt>
        <dd>{usage.paid_unlimited ? 'Unlimited' : usage.paid_balance ?? 'Unavailable'}</dd>
        <dt>Rate-limit reset credits</dt>
        <dd>{usage.reset_credits ?? 'Unavailable'}</dd>
      </dl>
    </>
  )
}

export default function CodexUsage() {
  const { data, retry, pending } = useResource('/account/codex-usage')
  if (!data) return null
  return (
    <section className="panel settings-panel" aria-labelledby="codex-usage-heading">
      <h2 id="codex-usage-heading">Codex quota and credits</h2>
      {data.usage ? <CodexUsageDetails usage={data.usage} /> : <p>Codex usage is unavailable.</p>}
      <button type="button" disabled={pending} onClick={retry}>{pending ? 'Refreshing…' : 'Refresh usage'}</button>
    </section>
  )
}
