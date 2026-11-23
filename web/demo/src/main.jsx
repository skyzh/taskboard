import React, { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import BoardViews from '../../src/BoardViews.jsx'
import { ReportTimeProvider } from '../../src/ReportTime.jsx'
import { AssigneeReportList, ReportsHeading, ReportsLayout } from '../../src/Reports.jsx'
import { QueuedAsksContent } from '../../src/QueuedAsks.jsx'
import { BoardBrand } from '../../src/shared.jsx'
import { useTheme } from '../../src/theme.js'
import { DemoControl } from '../../src/DemoControl.jsx'
import DemoNotice from './DemoNotice.jsx'
import { capturedAt, entries, profiles, queuedAsks, repository } from './snapshot.js'
import '../../src/styles.css'
import '../../src/night.css'
import './snapshot.css'

const tourTargets = { owner: 'snapshot-owner', current: 'snapshot-current', goals: 'snapshot-goals', outcomes: 'snapshot-outcomes' }
const requestTarget = 'snapshot-requests'
const hasRequests = queuedAsks && Object.values(queuedAsks).some(rows => rows.length > 0)
const setupPrompt = 'https://github.com/skyzh/taskboard/blob/main/docs/taskboard/SETUP.md#initial-agent-prompt'
const tour = [
  { id: tourTargets.owner, title: 'Who owns the work', text: 'Each agent’s report groups the work they own. The green dot shows who has something in progress.' },
  { id: tourTargets.current, title: 'Current work', text: 'Read the status, next step, and any blocker on a ticket that is still open.' },
  { id: tourTargets.goals, title: 'Goals', text: 'See what each person is responsible for improving.' },
  { id: tourTargets.outcomes, title: 'Recent outcomes', text: 'Read the latest results and open their public pull requests when available.' },
  ...(hasRequests ? [{ id: requestTarget, title: 'Request log', text: 'See what teammates asked for and who owns the next step.' }] : []),
  { title: 'Prompt your agents to use it!', text: 'Use the setup guide to connect your board and give each agent its first prompt.', action: setupPrompt },
]

const snapshotTime = new Intl.DateTimeFormat('en', {
  dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC',
}).format(new Date(capturedAt))

function SummaryDemo() {
  useTheme()
  const [step, setStep] = useState(null)
  const header = useRef(null)
  const trigger = useRef(null)
  const panel = useRef(null)
  const restoreFocus = useRef(false)
  const active = step == null ? null : tour[step]

  useEffect(() => {
    const element = header.current
    const style = document.documentElement.style
    const reserveSpace = () => style.setProperty('--app-header-height', `${element.getBoundingClientRect().height}px`)
    reserveSpace()
    const resize = new ResizeObserver(reserveSpace)
    resize.observe(element)
    return () => {
      resize.disconnect()
      style.removeProperty('--app-header-height')
    }
  }, [])

  useEffect(() => {
    if (!active) {
      if (restoreFocus.current) trigger.current?.focus()
      return
    }
    const target = active.id && document.getElementById(active.id)
    if (target instanceof HTMLDetailsElement) target.open = true
    panel.current?.focus({ preventScroll: true })
    if (!target) return
    window.scrollTo({
      top: window.scrollY + target.getBoundingClientRect().top - header.current.getBoundingClientRect().bottom - 8,
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
    })
  }, [active])

  function finish() {
    restoreFocus.current = true
    setStep(null)
  }

  return (
    <div onKeyDown={event => {
      if (event.key === 'Escape' && active) finish()
    }}>
      <header className="app-header" ref={header}>
        <div className="masthead demo-masthead">
          <BoardBrand href={repository} />
          <BoardViews current="reports" reportsHref="#reports" showNotifications preview />
          <DemoControl as="a" className="demo-repository" href={repository} aria-label="Taskboard on GitHub">
            <span aria-hidden="true" />
          </DemoControl>
        </div>
        <p className="appbar-connection snapshot-date">
          Snapshot from <time dateTime={capturedAt}>{snapshotTime} UTC</time>
        </p>
        {active && (
          <aside className="snapshot-tour" aria-labelledby="tour-title" tabIndex={-1} ref={panel}>
            <h2 id="tour-title">{active.title} <span>{step + 1} of {tour.length}</span></h2>
            <p>{active.text}</p>
            {active.action && <p><DemoControl as="a" href={active.action}>Open the agent setup guide</DemoControl></p>}
            <div className="snapshot-tour-actions">
              <DemoControl as="button" type="button" disabled={step === 0} onClick={() => setStep(step - 1)}>Back</DemoControl>
              <DemoControl as="button" type="button" onClick={() => step + 1 === tour.length ? finish() : setStep(step + 1)}>
                {step + 1 === tour.length ? 'Finish' : 'Next'}
              </DemoControl>
              {step + 1 < tour.length && <DemoControl as="button" type="button" onClick={finish}>Skip</DemoControl>}
            </div>
          </aside>
        )}
      </header>
      <main aria-label="Taskboard content">
        <div className="main-content snapshot">
          <p className="snapshot-project">Tasks Board</p>
          <section className="reports" aria-labelledby="reports">
            <ReportsHeading headingId="reports">
              <span>Team overview</span>
              <DemoControl as="button" className="snapshot-tour-trigger" ref={trigger} type="button"
                onClick={() => setStep(0)} hidden={Boolean(active)}>Start tour</DemoControl>
            </ReportsHeading>
            <ReportTimeProvider capturedAt={capturedAt}>
              <ReportsLayout sidebar={hasRequests && (
                <div id={requestTarget} data-tour-current={active?.id === requestTarget || undefined}>
                  <QueuedAsksContent snapshot={queuedAsks} snapshotProfiles={profiles} />
                </div>
              )}>
                <AssigneeReportList entries={entries} label="Taskboard squad Summary" publicPreview tourOwner="prism"
                  tourTargets={tourTargets} activeTarget={active?.id} />
              </ReportsLayout>
            </ReportTimeProvider>
          </section>
        </div>
      </main>
    </div>
  )
}

createRoot(document.getElementById('root')).render(<DemoNotice><SummaryDemo /></DemoNotice>)
