import React, { useState } from 'react'
import { request } from './data.js'

export function useTransitionRequest(onChanged, success) {
  const [saving, setSaving] = useState(false)
  const [feedback, setFeedback] = useState(null)
  const [forceReason, setForceReason] = useState('')
  const warning = feedback?.pendingRequest

  async function send(pendingRequest, override = '') {
    const { path, body, method } = pendingRequest
    setSaving(true)
    setFeedback(null)
    try {
      await request(path, {
        method,
        body: override ? { ...body, force: true, force_reason: override } : body,
      })
      if (override) setForceReason('')
      if (success) setFeedback({ text: success })
      onChanged()
    } catch (failure) {
      const confirmation = failure.status === 409 && failure.code === 'TRANSITION_CONFIRMATION_REQUIRED'
      setFeedback({ error: true, text: failure.message, ...(confirmation && { pendingRequest }) })
      if (override && !confirmation) setForceReason('')
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
      if (!saving && warning && forceReason.trim()) return send(warning, forceReason.trim())
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
      {feedback.pendingRequest && <>
        <label>Reason for overriding the warning
          <textarea value={forceReason} onChange={event => setForceReason(event.target.value)}
            rows={2} maxLength={4000} disabled={saving} />
        </label>
        <button type="button" disabled={saving || !forceReason.trim()} onClick={retry}>Apply override</button>
        <button type="button" disabled={saving} onClick={clear}>Cancel override</button>
      </>}
    </>
  )
}
