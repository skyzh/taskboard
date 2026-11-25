import React, { useLayoutEffect, useRef, useState } from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { request, useLoadedPages } from './data.js'
import { EvidenceLinks, LoadMore, ResourceStatus, TaskLink, Timestamp, eventHref, profileName, taskLabel } from './shared.jsx'
import { Markdown } from './Markdown.jsx'
import ReminderDate from './ReminderDate.jsx'
import { WaitAge } from './WaitAge.jsx'
import { projectFilters } from './projectScope.js'

export function AttentionReceipt({ receipt }) {
  return (
    <p role="status" className="section-message">
      Request {receipt.id} handled · <Timestamp at={receipt.handled_at} />
      {receipt.unblocked_task_id && (
        <> · Moved <TaskLink reference={receipt.unblocked_task_key} id={receipt.unblocked_task_id}
          title={receipt.unblocked_task_title} /> to Ready
          {receipt.ready_event_id && <> · <BoardLink href={eventHref(receipt.ready_event_id)}>View Ready event</BoardLink></>}
        </>
      )}
    </p>
  )
}

export function AttentionImpact({ item }) {
  const task = item.unblocks
  if (!task) return <p className="event-meta">For your information</p>
  if (item.handled_at && !task.ready_event_id) return null
  const link = <TaskLink reference={task.key} id={task.id} title={task.title} />
  return (
    <p className="event-meta">
      {item.handled_at
        ? <>Moved {link} to Ready · <BoardLink href={eventHref(task.ready_event_id)}>View Ready event</BoardLink></>
        : <>Unblocks {link}</>}
    </p>
  )
}

export default function AttentionRequests({ revision, onChanged, taskId, project, showControls = true }) {
  const onTicket = taskId != null
  const [state, setState] = useState('open')
  const [pending, setPending] = useState(null)
  const [notice, setNotice] = useState(null)
  const [handledIds, setHandledIds] = useState([])
  const [invalidatedPage, setInvalidatedPage] = useState(null)
  const page = useLoadedPages('/notifications/requests', {
    state: onTicket ? 'open' : state,
    task_id: taskId,
    ...projectFilters(onTicket ? undefined : project),
  }, revision)
  const latestPage = useRef(page.data)
  useLayoutEffect(() => {
    latestPage.current = page.data
  }, [page.data])
  function requestsChanged() {
    // A reminder can finish after a background refresh replaced the old page.
    if (onTicket) setInvalidatedPage(latestPage.current)
    onChanged()
  }
  function retryRequests() {
    if (onTicket) setInvalidatedPage(page.data)
    page.retry()
  }
  async function handle(id) {
    setPending(id)
    setNotice(null)
    try {
      const receipt = await request(`/notifications/requests/${id}/handled`, { method: 'POST' })
      if (onTicket) setHandledIds(previous => [...previous, id])
      setNotice({ receipt })
      requestsChanged()
    } catch (error) {
      setNotice({ error: error.message })
    } finally {
      setPending(null)
    }
  }
  const invalidated = onTicket && page.data != null && page.data === invalidatedPage
  const ticketError = page.error ?? (invalidated ? page.moreError : null)
  const hideTicketRows = onTicket && (ticketError || (invalidated && page.pending))
  const items = hideTicketRows ? [] : (page.data?.items ?? []).filter(item => !handledIds.includes(item.id))
  if (onTicket && items.length === 0 && !notice && !hideTicketRows) return null

  const content = (
    <>
      {!onTicket && (
        <div className="request-heading">
          <h3>Requests</h3>
          <label>Show <select value={state} onChange={event => setState(event.target.value)}>
            <option value="open">Open</option>
            <option value="deferred">Deferred</option>
            <option value="handled">Handled</option>
          </select></label>
        </div>
      )}
      {(!onTicket || hideTicketRows) && (
        <ResourceStatus resource={onTicket ? { ...page, data: undefined, error: ticketError, retry: retryRequests } : page}
          label="attention requests" />
      )}
      {!onTicket && page.data?.items.length === 0 && <p className="section-message">No {state} requests.</p>}
      {notice?.error && <p role="alert" className="section-message">{notice.error}</p>}
      {notice?.receipt && <AttentionReceipt receipt={notice.receipt} />}
      {items.length > 0 && <ol className="event-list" role="list">
        {items.map(item => (
          <li className="event-row attention-row" key={item.id}>
            <div>
              {!onTicket && <p className="event-ticket"><TaskLink reference={item.task_key} id={item.task_id} title={item.task_title} /></p>}
              <p className="event-meta">
                <span>{profileName(item.sender, item.sender_profile)}</span>
                {item.handled_at ? <Timestamp at={item.raised_at} /> : <WaitAge since={item.raised_at} label="Unanswered" />}
              </p>
              <Markdown text={item.reason} />
              <AttentionImpact item={item} />
              {item.remind_on && <p className="event-meta">Reminder <time dateTime={item.remind_on}>{item.remind_on}</time></p>}
              <EvidenceLinks links={item.links} />
            </div>
            {item.handled_at
              ? <span className="event-meta">Handled <Timestamp at={item.handled_at} /></span>
              : showControls && (
                <div>
                  <button type="button" disabled={pending !== null} onClick={() => handle(item.id)}>Mark handled</button>
                  <ReminderDate key={item.remind_on || ''} path={`/notifications/requests/${item.id}/reminder`}
                    saved={item.remind_on} onChanged={requestsChanged} />
                </div>
              )}
          </li>
        ))}
      </ol>}
      {!hideTicketRows && (
        <LoadMore page={page} label={onTicket ? `attention requests for ${taskLabel(taskId)}` : 'attention requests'} />
      )}
    </>
  )
  return onTicket ? <section className="ticket-attention" aria-label="Attention requests">{content}</section> : content
}
