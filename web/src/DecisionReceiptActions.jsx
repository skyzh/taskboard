import React, { useState } from 'react'
import { BoardLink, useBoardNavigation } from './BoardNavigation.jsx'
import { queryPath, request, useResource } from './data.js'
import { projectFilters, projectHref } from './projectScope.js'
import { taskHref } from './shared.jsx'

export function decisionTicketHref(task, project) {
  const context = new URLSearchParams({ 'decision-scope': project || 'all' })
  return `${taskHref(task.id, task.key)}?${context}`
}

export function decisionsHref(project) {
  return projectHref('#notifications-needs-me', project)
}

export default function DecisionReceiptActions({ project, revision }) {
  const path = queryPath('/notifications/decisions/next', projectFilters(project))
  const queue = useResource(path, revision)
  const navigate = useBoardNavigation()
  const [checking, setChecking] = useState(false)
  const [message, setMessage] = useState(null)
  const next = !queue.pending && !queue.error ? queue.data?.next : null

  async function openNext(event) {
    if (!navigate || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault()
    if (checking) return
    setChecking(true)
    setMessage(null)
    try {
      const current = await request(path, { cache: 'no-store' })
      if (current.next) navigate(decisionTicketHref(current.next, project))
      else {
        setMessage('No open decisions remain.')
        queue.retry()
      }
    } catch {
      setMessage('Could not refresh your decisions. Open the list or try again.')
      queue.retry()
    } finally {
      setChecking(false)
    }
  }

  return (
    <>
      {next && <BoardLink className="decision-return" href={decisionTicketHref(next, project)}
        aria-disabled={checking || undefined} onClick={openNext}>Next decision ({queue.data.count} in inbox)</BoardLink>}
      {' '}<BoardLink className="decision-return" href={decisionsHref(project)}>Return to decisions</BoardLink>
      {message && <p className="section-message" role="status">{message}</p>}
    </>
  )
}
