import React, { useEffect, useRef, useState } from 'react'
import { BoardLink, useBoardNavigation } from './BoardNavigation.jsx'
import { queryPath, request, useResource } from './data.js'
import { projectFilters, projectHref } from './projectScope.js'
import { taskHref } from './shared.jsx'

export function decisionTicketHref(task, project) {
  const context = new URLSearchParams({ 'decision-scope': project || 'all' })
  if (task.decision_request_id) context.set('decision-request', task.decision_request_id)
  return `${taskHref(task.id, task.key)}?${context}`
}

export function decisionsHref(project) {
  return projectHref('#notifications-needs-me', project)
}

export default function DecisionReceiptActions({ project, revision, showReturn = true }) {
  const path = queryPath('/notifications/decisions/next', projectFilters(project))
  const queue = useResource(path, revision)
  const navigate = useBoardNavigation()
  const pending = useRef(null)
  const [checking, setChecking] = useState(false)
  const [message, setMessage] = useState(null)
  const next = !queue.pending && !queue.error ? queue.data?.next : null

  useEffect(() => () => {
    pending.current?.abort()
    pending.current = null
  }, [path])

  function cancelNavigation() {
    pending.current?.abort()
    pending.current = null
    setChecking(false)
  }

  async function openNext() {
    if (!navigate || pending.current) return
    const active = new AbortController()
    pending.current = active
    setChecking(true)
    setMessage(null)
    try {
      const current = await request(path, { signal: active.signal, cache: 'no-store' })
      if (active.signal.aborted) return
      if (current.next) navigate(decisionTicketHref(current.next, project))
      else {
        setMessage('No open decisions remain.')
        queue.retry()
      }
    } catch {
      if (active.signal.aborted) return
      setMessage('Could not refresh your decisions. Open the list or try again.')
      queue.retry()
    } finally {
      if (pending.current === active) {
        pending.current = null
        setChecking(false)
      }
    }
  }

  return (
    <>
      {next && <button className="decision-return" type="button" disabled={checking} onClick={openNext}>
        Next decision ({queue.data.count} in inbox)
      </button>}
      {showReturn && <> <BoardLink className="decision-return" href={decisionsHref(project)} onClick={cancelNavigation}>Return to decisions</BoardLink></>}
      {message && <p className="section-message" role="status">{message}</p>}
    </>
  )
}
