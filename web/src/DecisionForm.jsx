import React, { useEffect, useRef, useState } from 'react'
import { decisionRequest, isUncertainDecision, watchDecision } from './decisionRecovery.js'
import { Markdown, MarkdownInline } from './Markdown.jsx'
import { eventHref, ResourceStatus, signInHref, taskHref } from './shared.jsx'

const emptyDraft = { choice: '', other: false, note: '', details: '' }

function recipient(session) {
  const actor = session.data?.actor
  return actor?.kind === 'human' ? actor.id ?? actor.name : null
}

function actionableRequestId(task, viewer) {
  if (!viewer) return null
  if (Object.hasOwn(task, 'decision_request_id')) return task.decision_request_id
  return task.state === 'human_review' && task.waiting_on === viewer ? task.event_id : null
}

export function mayAnswer(task, session) {
  return actionableRequestId(task, recipient(session)) != null
}

function DecisionResult({ attempt, draft }) {
  if (!attempt) return null
  const { status, answer } = attempt
  if (status === 'answered') {
    return (
      <div className="decision-result">
        <p role="status">Decision recorded. <a href={eventHref(answer.event_id)}>View your answer</a></p>
        {answer.choice && <p>Selected <MarkdownInline text={answer.choice} links={false} /></p>}
        <Markdown text={answer.note} />
      </div>
    )
  }
  if (status === 'saved') return <p role="status">Decision recorded.</p>
  if (status !== 'closed' && status !== 'unavailable') return null

  return (
    <>
      <p role="status">{status === 'closed'
        ? 'This decision request was closed elsewhere. Ask the task owner for a new request if a decision is still needed.'
        : 'Could not confirm that your answer was recorded. Check the task’s activity or sign in again.'}</p>
      {draft?.id === attempt.request.id && (
        <details data-reload-blocked="true">
          <summary>Your draft</summary>
          <textarea aria-label="Draft answer" rows={3} readOnly value={[draft.choice, draft.note, draft.details].filter(Boolean).join('\n\n')} />
        </details>
      )}
    </>
  )
}

export default function DecisionForm({ task, session, onChanged, onAnswerStarted }) {
  const [draft, setDraft] = useState(null)
  const [attempt, setAttempt] = useState(null)
  const posting = useRef(null)
  const viewer = recipient(session)
  const ownAttempt = viewer && attempt?.recipient === viewer ? attempt : null
  const awaiting = task?.state === 'human_review'
  const currentRequestId = task ? actionableRequestId(task, viewer) : undefined
  const lastKnownRequest = useRef(currentRequestId)
  const observedRequest = task ? currentRequestId : lastKnownRequest.current
  const staleOpen = ownAttempt?.status === 'open' && observedRequest !== undefined && observedRequest !== ownAttempt.request.id
  const checking = ownAttempt?.status === 'checking' || staleOpen ? ownAttempt : null

  useEffect(() => {
    if (currentRequestId !== undefined) lastKnownRequest.current = currentRequestId
  }, [currentRequestId])

  useEffect(() => () => {
    const active = posting.current
    posting.current = null
    active?.abort()
  }, [])

  useEffect(() => {
    if (!checking) return
    const { request } = checking
    return watchDecision(`${request.path}/${request.id}`, result => {
      const current = lastKnownRequest.current
      if (result.status === 'open' && current !== undefined && current !== request.id) return false
      posting.current = null
      setAttempt({ ...checking, ...result })
      if (result.status === 'answered') setDraft(null)
      if (result.status === 'answered' || result.status === 'closed') onChanged()
    })
  }, [checking, onChanged])

  const login = task && signInHref(taskHref(task.id, task.key))
  if (awaiting && session.error?.status === 401) return <p><a href={login}>Sign in to answer</a></p>
  if (awaiting && (session.pending || session.error) && !ownAttempt) {
    return <ResourceStatus resource={session} label="your session" />
  }

  const request = ['saving', 'checking', 'open'].includes(ownAttempt?.status) ? ownAttempt.request
    : currentRequestId != null ? {
      id: currentRequestId,
      path: `/tasks/${encodeURIComponent(task.key || task.id)}/decision`,
      choices: task.context?.choices || [],
    } : null
  const formAttempt = ownAttempt?.request.id === request?.id ? ownAttempt : null
  const showForm = request && !['answered', 'closed', 'unavailable', 'saved'].includes(formAttempt?.status)
  const { choice, other, note, details } = draft && draft.id === request?.id ? draft : emptyDraft
  const pending = formAttempt?.status === 'saving' || Boolean(checking)
  const retry = formAttempt?.status === 'open' && !checking
  const error = formAttempt?.error
  const incomplete = !note.trim() || Boolean(request?.choices.length && !choice && !other)

  function edit(field, value) {
    setDraft({ id: request.id, choice, other, note, details, [field]: value })
  }

  function select(choice, other) {
    setDraft({ id: request.id, choice, other, note, details })
  }

  async function submit(event) {
    event.preventDefault()
    const current = lastKnownRequest.current
    if (posting.current || pending || incomplete || (current !== undefined && current !== request.id)) return
    const active = new AbortController()
    const submission = { request, recipient: viewer }
    let awaitingReceipt = false
    posting.current = active
    setAttempt({ ...submission, status: 'saving' })
    onAnswerStarted?.()
    try {
      await decisionRequest(request.path, active, { body: {
        request_id: request.id, note: note.trim(), details: details.trim(),
        ...(other ? { other: true } : request.choices.length && { choice }),
      } })
      if (posting.current !== active) return
      setDraft(null)
      setAttempt({ ...submission, status: 'saved' })
      onChanged()
    } catch (failure) {
      if (posting.current !== active) return
      awaitingReceipt = isUncertainDecision(failure)
      setAttempt(awaitingReceipt
        ? { ...submission, status: 'checking' }
        : { ...submission, status: 'failed', error: failure })
    } finally {
      if (!awaitingReceipt && posting.current === active) posting.current = null
    }
  }

  return (
    <>
      <DecisionResult attempt={ownAttempt} draft={draft} />
      {showForm && (
        <form className="ownership-form decision-form" onSubmit={submit}
          data-reload-blocked={Boolean(pending || choice || other || note || details)}>
          {request.choices.length > 0 && (
            <fieldset disabled={pending}>
              <legend>Choose an option</legend>
              {request.choices.map(option => (
                <label key={option}>
                  <input type="radio" name="decision-choice" value={option}
                    checked={!other && option === choice} onChange={() => select(option, false)} required />
                  <span><MarkdownInline text={option} links={false} /></span>
                </label>
              ))}
              <label>
                <input type="radio" name="decision-choice" value=""
                  checked={Boolean(other)} onChange={() => select('', true)} required />
                <span>Other (write an answer)</span>
              </label>
            </fieldset>
          )}
          <label>
            Answer
            <textarea rows={4} value={note} aria-describedby={`decision-answer-format-${request.id}`}
              onChange={event => edit('note', event.target.value)} disabled={pending} required />
          </label>
          <p className="field-guidance-help" id={`decision-answer-format-${request.id}`}>Supports multiline text and Markdown.</p>
          <label>
            Details (optional)
            <textarea rows={3} value={details} aria-describedby={`decision-details-format-${request.id}`}
              onChange={event => edit('details', event.target.value)} disabled={pending} />
          </label>
          <p className="field-guidance-help" id={`decision-details-format-${request.id}`}>Block Markdown, including fenced Mermaid diagrams.</p>
          {checking && <p role="status">Connection interrupted. Checking whether your answer was recorded…</p>}
          {retry && <p role="status">This decision request is still open. Review your draft and retry.</p>}
          {error && <p role="alert">{error.message}{error.status === 401 && <> <a href={login}>Sign in</a></>}</p>}
          <button type="submit" disabled={pending || incomplete}>
            {formAttempt?.status === 'saving' ? 'Recording…' : retry ? 'Retry recording' : 'Record decision'}
          </button>
        </form>
      )}
    </>
  )
}
