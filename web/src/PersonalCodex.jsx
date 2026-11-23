import React, { useEffect, useRef, useState } from 'react'
import { request } from './data.js'
import { ReportTime } from './ReportTime.jsx'
import { ResourceStatus, signInHref, taskHref } from './shared.jsx'

export const personalCodexStatus = '/personal-codex/status'
const connectionsPath = '/personal-codex/connections'
const guide = 'https://github.com/skyzh/taskboard/blob/main/docs/taskboard/PERSONAL-CODEX.md'

function CopyPrompt({ ticket, needsConnection }) {
  const base = new URL('./', window.location.href).href
  const prompt = `Read ${guide} and follow it to report progress on ${ticket} using ${base}`
  const [feedback, setFeedback] = useState('')

  async function copy() {
    try {
      await navigator.clipboard.writeText(prompt)
      setFeedback('Prompt copied.')
    } catch {
      setFeedback('Select the prompt text to copy it.')
    }
  }

  return (
    <>
      <p className="personal-codex-prompt"><code>{prompt}</code></p>
      {needsConnection && (
        <p className="personal-codex-preparation">
          Before using this prompt, create a connection below and set its token in your Codex session’s environment.
        </p>
      )}
      <button type="button" onClick={copy}>Copy prompt</button>
      <p className="personal-codex-feedback" role="status">{feedback}</p>
    </>
  )
}

function Connections({ connections, reload }) {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [token, setToken] = useState('')
  const [feedback, setFeedback] = useState(null)
  const tokenInput = useRef(null)
  const nameInput = useRef(null)

  useEffect(() => {
    if (token) tokenInput.current?.focus()
  }, [token])

  async function create(event) {
    event.preventDefault()
    if (busy || token || !name.trim()) return
    setBusy(true)
    setFeedback(null)
    try {
      const grant = await request(connectionsPath, { body: { session: name.trim() } })
      setName('')
      setToken(grant.token)
      reload()
    } catch (failure) {
      setFeedback({ error: true, text: failure.message })
    } finally {
      setBusy(false)
    }
  }

  async function revoke(id, button) {
    if (busy) return
    setBusy(true)
    setFeedback(null)
    try {
      await request(`${connectionsPath}/${encodeURIComponent(id)}`, { method: 'DELETE' })
      if (document.activeElement === button || document.activeElement === document.body) nameInput.current?.focus()
      setFeedback({ text: 'Connection revoked.' })
      reload()
    } catch (failure) {
      setFeedback({ error: true, text: failure.message })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel settings-panel" aria-labelledby="codex-connections">
      <h2 id="codex-connections">Session connections</h2>
      <p>Each session needs its own connection. Connections expire after eight hours.</p>
      <form className="profile-form" onSubmit={create} data-reload-blocked={Boolean(busy || name || token)}>
        <label>Session name
          <input ref={nameInput} name="session" value={name} onChange={event => setName(event.target.value)}
            aria-required="true" maxLength={80} autoComplete="off" readOnly={busy || Boolean(token)} />
        </label>
        <button type="submit" disabled={busy || Boolean(token) || !name.trim()}>Create connection</button>
      </form>
      {token && (
        <div className="profile-form personal-codex-token">
          <label>Copy this token now. It is shown once.
            <input ref={tokenInput} value={token} onFocus={event => event.target.select()} readOnly
              autoComplete="off" spellCheck={false} />
          </label>
          <p>Set <code>TASKBOARD_PERSONAL_CODEX_TOKEN</code> in the Codex session environment.</p>
          <button type="button" onClick={() => { setToken(''); nameInput.current?.focus() }}>Hide token</button>
        </div>
      )}
      {feedback && <p className="personal-codex-feedback" role={feedback.error ? 'alert' : 'status'}>{feedback.text}</p>}
      <h3 className="personal-codex-active">Active connections</h3>
      {connections.length === 0 ? <p>No active connections.</p> : (
        <ul className="personal-codex-connections" role="list">
          {connections.map(({ id, label, expires_at }) => (
            <li key={id}>
              <span><strong>{label}</strong><br />Expires <ReportTime at={expires_at} /></span>
              <button type="button" disabled={busy || Boolean(token)} aria-label={`Revoke ${label}`}
                onClick={event => revoke(id, event.currentTarget)}>Revoke</button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

export default function PersonalCodex({ setup, session, reportsHref }) {
  const actor = session.data?.actor
  const unavailable = [403, 404].includes(setup.error?.status)
  const signIn = setup.error?.status === 401 || ((!actor || actor.kind === 'portal') &&
    (session.data?.sign_in_available || session.error?.status === 401))
  const pendingSession = session.pending && !actor
  const pilot = signIn || unavailable ? null : setup.data

  return (
    <section className="settings personal-codex" aria-labelledby="personal-codex">
      <a className="back-link" href={reportsHref}>Back to Reports</a>
      <div className="page-heading"><h1 id="personal-codex" tabIndex={-1}>Personal Codex</h1></div>
      {signIn ? <a href={signInHref('#personal-codex')}>Sign in with Raft</a>
        : pendingSession || (actor?.kind === 'human' && !unavailable)
          ? <ResourceStatus resource={pendingSession ? session : setup} label="Personal Codex setup" />
          : <p>Personal Codex is unavailable for this account.</p>}
      {pilot && (
        <>
          <section className="panel settings-panel" aria-labelledby="codex-prompt">
            <h2 id="codex-prompt">Reporting prompt</h2>
            <p>Codex reports appear in <a href={taskHref(pilot.ticket)}>{pilot.ticket}</a> Activity.</p>
            {pilot.reporting_auth === 'tailnet' && <p>Sessions on the tailnet can report without a token.</p>}
            <CopyPrompt ticket={pilot.ticket} needsConnection={pilot.reporting_auth === 'login_required'} />
          </section>
          {pilot.reporting_auth === 'login_required' &&
            <Connections connections={pilot.connections || []} reload={setup.retry} />}
        </>
      )}
    </section>
  )
}
