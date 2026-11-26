import React, { useState } from 'react'
import { request } from './data.js'

export const recommendedTextLength = 4000

function textLengthWarning(error) {
  if (error.status !== 400 || error.code !== 'TEXT_LENGTH_CONFIRMATION_REQUIRED') return null
  return error.limit
    ? `This draft exceeds the recommended length of ${error.limit.toLocaleString()} characters.`
    : 'This draft exceeds the recommended length.'
}

export function useTransitionRequest(onChanged, success) {
  const [saving, setSaving] = useState(false)
  const [feedback, setFeedback] = useState(null)
  const [forceReason, setForceReason] = useState('')
  const warning = feedback?.pendingRequest

  async function send(pendingRequest) {
    const { path, body, method } = pendingRequest
    setSaving(true)
    setFeedback(null)
    try {
      await request(path, { method, body })
      if (forceReason) setForceReason('')
      if (success) setFeedback({ text: success })
      onChanged()
    } catch (failure) {
      const textWarning = !body.force_text && textLengthWarning(failure)
      const transitionWarning = failure.status === 409 && failure.code === 'TRANSITION_CONFIRMATION_REQUIRED'
      setFeedback({
        error: true, text: textWarning || failure.message,
        ...((textWarning || transitionWarning) && { pendingRequest }),
        ...(textWarning && { confirmTextLength: true }),
      })
      if (forceReason && !textWarning && !transitionWarning) setForceReason('')
    } finally {
      setSaving(false)
    }
  }

  return {
    saving, feedback, forceReason, setForceReason,
    blocked: saving || Boolean(warning),
    submit(path, body, method = 'POST') {
      if (!saving && !warning) return send({ path, body, method })
    },
    retry() {
      if (saving || !warning) return
      if (feedback.confirmTextLength) return send({ ...warning, body: { ...warning.body, force_text: true } })
      const reason = forceReason.trim()
      if (reason) {
        const body = { ...warning.body, force: true, force_reason: reason }
        if (body.force_text && [...reason].length > recommendedTextLength) delete body.force_text
        return send({ ...warning, body })
      }
    },
    clear() {
      setFeedback(null)
      if (forceReason) setForceReason('')
    },
  }
}

export default function TransitionFeedback({ mutation }) {
  const { feedback, saving, forceReason, setForceReason, retry, clear } = mutation
  if (!feedback) return null
  return (
    <>
      <p role={feedback.error ? 'alert' : 'status'}>{feedback.text}</p>
      {feedback.confirmTextLength ? <>
        <button type="button" disabled={saving} onClick={retry}>Keep full text</button>
        <button type="button" disabled={saving} onClick={clear}>Edit text</button>
      </> : feedback.pendingRequest && <>
        <label>Reason for overriding the warning
          <textarea value={forceReason} onChange={event => setForceReason(event.target.value)}
            rows={2} disabled={saving} />
        </label>
        <button type="button" disabled={saving || !forceReason.trim()} onClick={retry}>Apply override</button>
        <button type="button" disabled={saving} onClick={clear}>Cancel override</button>
      </>}
    </>
  )
}
