import React, { useContext, useLayoutEffect, useRef, useState } from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { request, useLoadedPages } from './data.js'
import { EvidenceLinks, LoadMore, ResourceStatus, TaskLink, Timestamp, eventHref, profileName, taskHref, taskLabel } from './shared.jsx'
import { Markdown } from './Markdown.jsx'
import ReminderDate from './ReminderDate.jsx'
import { WaitAge } from './WaitAge.jsx'
import { ReportTime } from './ReportTime.jsx'
import RequestKind from './RequestKind.jsx'
import { BoardOutageContext } from './BoardOutage.jsx'
import { isBackendError } from './boardAvailability.js'
import { projectFilters } from './projectScope.js'

export function attentionTicketHref(taskId, reference, requestId) {
  return `${taskHref(taskId, reference)}?attention-request=${requestId}`
}

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
  if (!task) return null
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

export default function AttentionRequests({ revision, onChanged, onRequestLoaded, taskId, project, requestId, onlyOpen = false, askState }) {
  const onTicket = taskId != null
  const [selectedState, setState] = useState('open')
  const state = askState || selectedState
  const [pending, setPending] = useState(null)
  const [notice, setNotice] = useState(null)
  const [handledIds, setHandledIds] = useState([])
  const [invalidatedPage, setInvalidatedPage] = useState(null)
  const page = useLoadedPages('/notifications/requests', {
    state: onTicket || onlyOpen ? 'open' : state,
    task_id: taskId,
    ...(requestId && { request_id: requestId }),
    ...projectFilters(onTicket ? undefined : project),
  }, revision)
  const latestPage = useRef(page.data)
  const requestFocused = useRef(false)
  useLayoutEffect(() => {
    latestPage.current = page.data
  }, [page.data])
  useLayoutEffect(() => {
    if (requestFocused.current || !onRequestLoaded || page.pending || (!page.data && !page.error)) return
    requestFocused.current = true
    onRequestLoaded()
  }, [onRequestLoaded, page.data, page.error, page.pending])
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
      setHandledIds(previous => [...previous, id])
      setNotice({ receipt })
      requestsChanged()
    } catch (error) {
      setNotice({ error: error.message, id })
    } finally {
      setPending(null)
    }
  }
  const invalidated = onTicket && page.data != null && page.data === invalidatedPage
  const ticketError = page.error ?? (invalidated ? page.moreError : null)
  const outage = useContext(BoardOutageContext)
  const keepTicketRows = outage && page.data != null && (!ticketError || isBackendError(ticketError))
  const hideTicketRows = onTicket && !keepTicketRows && (ticketError || (invalidated && page.pending))
  const items = hideTicketRows ? [] : (page.data?.items ?? []).filter(item => item.handled_at || !handledIds.includes(item.id))
  if (onTicket && items.length === 0 && !notice && !hideTicketRows && !requestId) return null

  if (onlyOpen && page.data && !page.error && items.length === 0 && !notice) return null

  const content = (
    <>
      {!onTicket && (
        <div className="request-heading">
          <h3>{askState === 'deferred' ? 'Deferred attention requests' : 'Requests'}</h3>
          {!onlyOpen && !askState && <label>Show <select value={state} onChange={event => setState(event.target.value)}>
            <option value="open">Open</option>
            <option value="deferred">Deferred</option>
            <option value="handled">Handled</option>
          </select></label>}
        </div>
      )}
      {(!onTicket || hideTicketRows || requestId) && (
        <ResourceStatus resource={onTicket ? { ...page, data: undefined, error: ticketError, retry: retryRequests } : page}
          label="attention requests" />
      )}
      {requestId && !page.pending && !ticketError && items.length === 0 && !notice && (
        <p className="section-message">This request is unavailable.</p>
      )}
      {!onTicket && !onlyOpen && !page.error && page.data?.items.length === 0 && <p className="section-message">No {state} requests.</p>}
      {notice?.receipt && (requestId && !notice.receipt.unblocked_task_id
        ? <p className="visually-hidden" role="status">Request handled.</p>
        : <AttentionReceipt receipt={notice.receipt} />)}
      {items.length > 0 && <ol className="event-list" role="list">
        {items.map(item => {
          const kind = item.intent || (item.unblocks || item.rollout_check ? 'ask' : 'fyi')
          return (
            <li className="event-row attention-row" key={item.id}>
              <div>
                {!onTicket && <p className="event-ticket"><TaskLink reference={item.task_key} id={item.task_id} title={item.task_title}
                  publicHref={attentionTicketHref(item.task_id, item.task_key, item.id)} /></p>}
                <p className="event-meta">
                  <RequestKind kind={kind} />
                  {item.handled_at ? <Timestamp at={item.raised_at} /> : kind === 'fyi' || requestId
                    ? <ReportTime at={item.raised_at} linked /> : <WaitAge since={item.raised_at} label="Unanswered" />}
                  <span>{item.rollout_check ? 'Rollout check' : profileName(item.sender, item.sender_profile)}</span>
                </p>
                <Markdown text={item.reason} />
                <AttentionImpact item={item} />
                {item.remind_on && <p className="event-meta">Reminder <time dateTime={item.remind_on}>{item.remind_on}</time>{askState && ' (Pacific time)'}</p>}
                <EvidenceLinks links={item.links} />
              </div>
              {item.handled_at
                ? <span className="event-meta">Handled <Timestamp at={item.handled_at} /></span>
                : !item.rollout_check && (
                  <div>
                    {notice?.id === item.id && <p role="alert" className="section-message">{notice.error}</p>}
                    <button type="button" aria-busy={pending === item.id || undefined}
                      disabled={pending !== null} onClick={() => handle(item.id)}>Mark handled</button>
                    <ReminderDate key={item.remind_on || ''} path={`/notifications/requests/${item.id}/reminder`}
                      saved={item.remind_on} deferAsk={Boolean(askState)} onChanged={requestsChanged} />
                  </div>
                )}
            </li>
          )
        })}
      </ol>}
      {!hideTicketRows && (
        <LoadMore page={page} label={onTicket ? `attention requests for ${taskLabel(taskId)}` : `${state} attention requests`} />
      )}
    </>
  )
  return onTicket ? <section className="ticket-attention" aria-label={requestId ? "Inbox request" : "Attention requests"}>
    {requestId && <h3>Inbox request</h3>}
    {content}
  </section> : content
}
