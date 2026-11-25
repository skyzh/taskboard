import React, { useEffect, useRef, useState } from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { request, useResource } from './data.js'
import { ReportTime } from './ReportTime.jsx'
import { ResourceStatus, useSignInHref, taskHref } from './shared.jsx'

export const personalCodexStatus = '/personal-codex/status'
const connectionsPath = '/personal-codex/connections'
const guide = 'https://github.com/skyzh/taskboard/blob/main/docs/taskboard/PERSONAL-CODEX.md'

export function CopyPrompt({ prompt, children }) {
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
      {children}
      <button type="button" onClick={copy}>Copy prompt</button>
      <p className="personal-codex-feedback" role="status">{feedback}</p>
    </>
  )
}

export function SessionConnections({ connections, reload, path, tokenVariable, expiration }) {
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
      const grant = await request(path, { body: { session: name.trim() } })
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
      await request(`${path}/${encodeURIComponent(id)}`, { method: 'DELETE' })
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
      <p>{expiration}</p>
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
          <p>Set <code>{tokenVariable}</code> in the session environment.</p>
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

export default function PersonalCodex({ session, reportsHref }) {
  const login = useSignInHref('#personal-codex')
  const actor = session.data?.actor
  const setup = useResource(actor?.kind === 'human' ? personalCodexStatus : null)
  const unavailable = [403, 404].includes(setup.error?.status)
  const signIn = setup.error?.status === 401 || ((!actor || actor.kind === 'portal') &&
    (session.data?.sign_in_available || session.error?.status === 401))
  const awaitingSession = !actor && (session.pending || (session.error && session.error.status !== 401))
  const pilot = signIn || unavailable ? null : setup.data
  const prompt = pilot && `Read ${guide} and follow it to report progress on ${pilot.ticket} using ${new URL('./', document.baseURI)}`

  return (
    <section className="settings personal-codex" aria-labelledby="personal-codex">
      <BoardLink className="back-link" href={reportsHref}>Back to Reports</BoardLink>
      <div className="page-heading"><h1 id="personal-codex" tabIndex={-1}>Personal Agents pilot</h1></div>
      {signIn ? <a href={login}>Sign in with Raft</a>
        : awaitingSession || (actor?.kind === 'human' && !unavailable)
          ? <ResourceStatus resource={awaitingSession ? session : setup} label="Personal Codex setup" />
          : <p>Personal Codex is unavailable for this account.</p>}
      {pilot && (
        <>
          <section className="panel settings-panel" aria-labelledby="codex-prompt">
            <h2 id="codex-prompt">Reporting prompt</h2>
            <p>Codex reports appear in <BoardLink href={taskHref(pilot.ticket)}>{pilot.ticket}</BoardLink> Activity.</p>
            {pilot.reporting_auth === 'tailnet' && <p>Sessions on the tailnet can report without a token.</p>}
            <CopyPrompt prompt={prompt}>
              {pilot.reporting_auth === 'login_required' && (
                <p className="personal-codex-preparation">
                  Before using this prompt, create a connection below and set its token in your session’s environment.
                </p>
              )}
            </CopyPrompt>
          </section>
          {pilot.reporting_auth === 'login_required' &&
            <SessionConnections connections={pilot.connections || []} reload={setup.retry}
              path={connectionsPath} tokenVariable="TASKBOARD_PERSONAL_CODEX_TOKEN"
              expiration="Each session needs its own connection. Connections expire after eight hours." />}
        </>
      )}
    </section>
  )
}
