import React, { useEffect, useEffectEvent, useId, useRef, useState } from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { queryPath, request, useLoadedPages, useResource } from './data.js'
import { LoadMore, ResourceStatus, TaskLink, Timestamp, useSignInHref } from './shared.jsx'

const actions = { claim: 'Claimed', renew: 'Renewed', release: 'Released', expire: 'Expired' }
const ticketKey = /^[A-Z][A-Z0-9]{0,7}-[1-9]\d{0,18}$/
const identity = () => crypto.randomUUID()

export function scheduleResourceExpiry(data, refreshedThrough, refresh) {
  const asOf = Date.parse(data.as_of)
  if (!Number.isFinite(asOf)) return
  let earliest = Infinity
  for (const { claim } of data.items) {
    const expiry = Date.parse(claim?.expires_at)
    if (expiry > asOf && expiry > refreshedThrough) earliest = Math.min(earliest, expiry)
  }
  if (!Number.isFinite(earliest)) return

  // Servers report whole seconds. Leave time for that clock to cross the boundary.
  const maxTimeout = 2 ** 31 - 1
  let timer
  function arm(remaining) {
    const delay = Math.min(remaining, maxTimeout)
    timer = setTimeout(() => {
      if (remaining > delay) arm(remaining - delay)
      else refresh(earliest)
    }, delay)
  }
  arm(earliest - asOf + 1000)
  return () => clearTimeout(timer)
}

export function resourceRequest(previous, path, body, createKey = identity) {
  const signature = JSON.stringify([path, body])
  return previous?.signature === signature ? previous : { signature, key: createKey() }
}

export function matchesResourceReadback(data, project, key, operation, submitted, receipt) {
  if (data.project !== project) return false
  const resource = data.items.find(item => item.key === key)
  if (!resource) return false
  if (operation === 'define') {
    return resource.title === submitted.title && resource.conditions === submitted.conditions
      && resource.max_minutes === submitted.max_minutes
  }
  if (receipt?.project !== project || receipt.key !== key) return false
  if (operation === 'release') return resource.claim === null
  return Boolean(receipt.claim && resource.claim && resource.claim.claim_id === receipt.claim.claim_id
    && resource.claim.revision === receipt.claim.revision)
}

function useResourceWrite({ project, listPath, onReadback, onReadFailure }) {
  const saving = useRef(false)
  const retry = useRef(null)
  const [pending, setPending] = useState(false)
  const [result, setResult] = useState(null)

  async function save(key, operation, submitted) {
    if (saving.current) return false
    saving.current = true
    setPending(true)
    setResult(null)
    const defining = operation === 'define'
    const source = `${listPath}/${encodeURIComponent(key)}`
    const path = defining ? source : `${source}/${operation}`
    let written = false
    try {
      let body = submitted
      if (!defining) {
        retry.current = resourceRequest(retry.current, path, submitted)
        body = { ...submitted, request_key: retry.current.key }
      }
      const receipt = await request(path, { method: defining ? 'PUT' : 'POST', body })
      written = true
      retry.current = null
      const data = await request(listPath, { cache: 'no-store' })
      const verified = matchesResourceReadback(data, project, key, operation, submitted, receipt)
      onReadback(data)
      setResult({ message: verified
        ? defining ? 'Resource saved.' : `Resource ${actions[operation].toLowerCase()}.`
        : 'Change saved. The current resource differs from the saved result.' })
      return verified
    } catch (error) {
      setResult(written
        ? { message: 'Change saved. The updated resource could not be loaded.' }
        : { message: error.message, error: true })
      if (written || [401, 403, 404, 409].includes(error.status)) onReadFailure()
      return false
    } finally {
      saving.current = false
      setPending(false)
    }
  }
  return { pending, result, save }
}

function WriteResult({ result }) {
  return result && <p role={result.error ? 'alert' : 'status'}>{result.message}</p>
}

function ClaimLink({ claim }) {
  if (ticketKey.test(claim.ticket || '')) return <TaskLink reference={claim.ticket} />
  if (claim.url && URL.canParse(claim.url)) {
    const link = new URL(claim.url)
    if (link.protocol === 'https:' && !link.username && !link.password) {
      return <BoardLink href={link.href}>Pull request</BoardLink>
    }
  }
  return 'Unavailable link'
}

export function ResourceClaimDetails({ claim }) {
  if (!claim) return <p className="schedule-muted">Available</p>
  return (
    <dl className="schedule-summary">
      <div><dt>Holder</dt><dd>{claim.holder?.name || 'Unavailable assignee'}</dd></div>
      <div><dt>Linked work</dt><dd><ClaimLink claim={claim} /></dd></div>
      <div><dt>Claimed</dt><dd><Timestamp at={claim.claimed_at} /></dd></div>
      <div><dt>Expires</dt><dd><Timestamp at={claim.expires_at} /></dd></div>
    </dl>
  )
}

export function ResourceHistoryRows({ items }) {
  if (!items.length) return <p className="schedule-muted">No claim history yet.</p>
  return (
    <ol className="resource-history-list">
      {items.map(item => (
        <li key={item.event_id}>
          <p><strong>{actions[item.action]}</strong> · <Timestamp at={item.at} />
            {item.actor?.name && <> · {item.actor.name}</>}</p>
          {item.claim && <ResourceClaimDetails claim={item.claim} />}
        </li>
      ))}
    </ol>
  )
}

function ResourceHistory({ path, revision }) {
  const [open, setOpen] = useState(false)
  const page = useLoadedPages(`${path}/history`, {}, revision, { limit: 10, enabled: open })
  return (
    <details onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>Claim history</summary>
      {open && <>
        <ResourceStatus resource={page} label="claim history" />
        {page.data && <ResourceHistoryRows items={page.data.items} />}
        <LoadMore page={page} label="claim history" />
      </>}
    </details>
  )
}

function DefinitionFields({ resource, pending, save }) {
  return (
    <form className="ownership-form resource-form" onSubmit={async event => {
      event.preventDefault()
      const form = event.currentTarget
      const data = new FormData(form)
      const key = resource?.key || data.get('key').trim()
      const saved = await save(key, 'define', {
        title: data.get('title').trim(), conditions: data.get('conditions').trim(),
        max_minutes: Number(data.get('max_minutes')),
      })
      if (saved && !resource) form.reset()
    }}>
      {!resource && <label>Resource key <input name="key" required pattern="[a-z][a-z0-9_\-]{0,63}"
        autoCapitalize="none" spellCheck={false} disabled={pending} /></label>}
      <label>Title <input name="title" required defaultValue={resource?.title} disabled={pending} /></label>
      <label>Conditions <textarea name="conditions" rows={4} required defaultValue={resource?.conditions}
        disabled={pending} /></label>
      <label>Maximum claim (minutes) <input name="max_minutes" type="number" min="1" required
        defaultValue={resource?.max_minutes} disabled={pending} /></label>
      <button type="submit" disabled={pending}>{resource ? 'Save definition' : 'Define resource'}</button>
    </form>
  )
}

function ClaimControls({ resource, pending, save }) {
  const [kind, setKind] = useState('ticket')
  const { key, claim, max_minutes: maxMinutes } = resource
  return (
    <div className="resource-controls" data-reload-blocked={pending || undefined}>
      {resource.can_claim === true && (
        <form className="ownership-form resource-form" onSubmit={event => {
          event.preventDefault()
          const fields = new FormData(event.currentTarget)
          if (fields.get('checked_conditions') !== 'on') return
          save(key, 'claim', {
            minutes: Number(fields.get('minutes')), checked_conditions: true,
            [kind === 'ticket' ? 'ticket' : 'pr_url']: fields.get('reference').trim(),
          })
        }}>
          <label>Link type <select value={kind} disabled={pending} onChange={event => setKind(event.target.value)}>
            <option value="ticket">Ticket</option>
            <option value="pr">Pull request</option>
          </select></label>
          {kind === 'ticket'
            ? <label key="ticket">Ticket key <input name="reference" required disabled={pending} placeholder="T-123" /></label>
            : <label key="pr">Pull request URL <input name="reference" type="url" required disabled={pending} /></label>}
          <label>Claim duration (minutes) <input name="minutes" type="number" min="1"
            max={maxMinutes} required disabled={pending} /></label>
          <label className="resource-checkbox"><input name="checked_conditions" type="checkbox" required disabled={pending} />
            I checked the claim conditions.</label>
          <button type="submit" disabled={pending}>Claim resource</button>
        </form>
      )}
      {resource.can_renew === true && claim && (
        <form className="ownership-form resource-form" onSubmit={event => {
          event.preventDefault()
          save(key, 'renew', {
            claim_id: claim.claim_id, revision: claim.revision,
            minutes: Number(new FormData(event.currentTarget).get('minutes')),
          })
        }}>
          <label>Renewal duration (minutes) <input name="minutes" type="number" min="1"
            max={maxMinutes} required disabled={pending} /></label>
          <button type="submit" disabled={pending}>Renew claim</button>
        </form>
      )}
      {resource.can_release === true && claim && <button type="button" disabled={pending}
        onClick={() => save(key, 'release', { claim_id: claim.claim_id, revision: claim.revision })}>Release claim</button>}
    </div>
  )
}

function ResourceCard({ resource, canDefine, revision, busy, writeContext }) {
  const label = useId()
  const writer = useResourceWrite(writeContext)
  const pending = busy || writer.pending
  return (
    <section className="panel resource-panel" aria-labelledby={label}>
      <header><h2 id={label}>{resource.title}</h2><p className="schedule-muted">{resource.key}</p></header>
      <div><h3>Claim conditions</h3><p className="resource-conditions">{resource.conditions}</p></div>
      <p className="schedule-muted">Maximum claim · {resource.max_minutes} minutes</p>
      <ResourceClaimDetails claim={resource.claim} />
      {canDefine && <details><summary>Edit definition</summary>
        <DefinitionFields resource={resource} pending={pending} save={writer.save} />
      </details>}
      <ClaimControls key={JSON.stringify([resource.conditions, resource.claim?.revision])}
        resource={resource} pending={pending} save={writer.save} />
      <WriteResult result={writer.result} />
      <ResourceHistory path={`${writeContext.listPath}/${encodeURIComponent(resource.key)}`} revision={revision} />
    </section>
  )
}

function DefineResource({ busy, writeContext }) {
  const writer = useResourceWrite(writeContext)
  return (
    <details className="panel resource-panel">
      <summary>Define resource</summary>
      <DefinitionFields pending={busy || writer.pending} save={writer.save} />
      <WriteResult result={writer.result} />
    </details>
  )
}

export function ResourcesContents({ data, revision, busy, writeContext }) {
  return (
    <div className="resource-list">
      {data.can_define === true && <DefineResource busy={busy} writeContext={writeContext} />}
      {data.items.map(resource => <ResourceCard key={resource.key} resource={resource}
        canDefine={data.can_define === true} revision={revision} busy={busy} writeContext={writeContext} />)}
      {!data.items.length && <p className="section-message">No resources in this project.</p>}
    </div>
  )
}

export default function Resources({ project, projectPicker, revision, session, verified, projectsPending }) {
  const login = useSignInHref(queryPath('#resources', { project }))
  const listPath = `/projects/${encodeURIComponent(project)}/resources`
  const resource = useResource(verified && !projectsPending ? listPath : null, revision)
  const [checked, setChecked] = useState(null)
  const [historyRevision, setHistoryRevision] = useState(0)
  const refreshedThrough = useRef(-Infinity)
  const data = checked?.revision === revision && !resource.error ? checked.data : resource.data
  const refreshClaims = () => {
    setChecked(null)
    resource.retry()
    setHistoryRevision(value => value + 1)
  }
  const refreshAfterExpiry = useEffectEvent(expiry => {
    refreshedThrough.current = expiry
    refreshClaims()
  })
  useEffect(() => {
    if (data?.project !== project || !verified || projectsPending || resource.pending || resource.error) return
    return scheduleResourceExpiry(data, refreshedThrough.current, expiry => refreshAfterExpiry(expiry))
  }, [data, project, verified, projectsPending, resource.pending, resource.error])
  const writeContext = {
    project, listPath,
    onReadback: readback => {
      if (readback.project === project) setChecked({ data: readback, revision })
      else resource.retry()
      setHistoryRevision(value => value + 1)
    },
    onReadFailure: refreshClaims,
  }
  return (
    <>
      <div className="page-heading schedule-heading">
        <h1>Resources</h1>{projectPicker}
        {verified && <button type="button" disabled={resource.pending || projectsPending}
          onClick={() => {
            refreshedThrough.current = -Infinity
            refreshClaims()
          }}>Refresh claims</button>}
        <BoardLink href={'#' + new URLSearchParams({ project })}>Back to Tasks</BoardLink>
      </div>
      {verified ? (
        <>
          <ResourceStatus resource={projectsPending ? { pending: true } : resource} label="project resources" />
          {data?.project === project && <ResourcesContents data={data} revision={`${revision}:${historyRevision}`}
            busy={resource.pending || Boolean(resource.error)} writeContext={writeContext} />}
        </>
      ) : session.pending ? <ResourceStatus resource={session} label="resources" />
        : <p>{session.data?.sign_in_available || session.error?.status === 401
          ? <a href={login}>Sign in</a> : 'Resources are available on the signed-in board.'}</p>}
    </>
  )
}
