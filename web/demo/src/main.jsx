import React, { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import BoardViews from '../../src/BoardViews.jsx'
import { ReportTimeProvider } from '../../src/ReportTime.jsx'
import { AssigneeReportList, ReportsHeading, ReportsLayout } from '../../src/Reports.jsx'
import { QueuedAsksContent } from '../../src/QueuedAsks.jsx'
import { BoardBrand } from '../../src/shared.jsx'
import { useTheme } from '../../src/theme.js'
import { capturedAt, entries, queuedAsks, repository } from './snapshot.js'
import '../../src/styles.css'
import '../../src/night.css'
import './snapshot.css'

const tour = [
  { id: 'snapshot-current', title: 'Current work', text: 'Read the next step and status on a ticket that is still open.' },
  { id: 'snapshot-goals', title: 'Goals', text: 'See what each person is responsible for improving.' },
  { id: 'snapshot-outcomes', title: 'Recent outcomes', text: 'Read the latest results and open their public pull requests when available.' },
]

const snapshotTime = new Intl.DateTimeFormat('en', {
  dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC',
}).format(new Date(capturedAt))

const tourTargets = { current: tour[0].id, goals: tour[1].id, outcomes: tour[2].id }

function SummaryDemo() {
  useTheme()
  const [step, setStep] = useState(null)
  const header = useRef(null)
  const trigger = useRef(null)
  const panel = useRef(null)
  const restoreFocus = useRef(false)
  const active = step == null ? null : tour[step]

  useEffect(() => {
    if (!active) {
      if (restoreFocus.current) trigger.current?.focus()
      return
    }
    const target = document.getElementById(active.id)
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
        <div className="masthead">
          <BoardBrand href={repository} />
          <BoardViews current="reports" reportsHref="#reports" showNotifications preview />
        </div>
        <p className="appbar-connection snapshot-date">
          Snapshot from <time dateTime={capturedAt}>{snapshotTime} UTC</time>
        </p>
        {active && (
          <aside className="snapshot-tour" aria-labelledby="tour-title" tabIndex={-1} ref={panel}>
            <h2 id="tour-title">{active.title} <span>{step + 1} of {tour.length}</span></h2>
            <p>{active.text}</p>
            <div className="snapshot-tour-actions">
              <button type="button" disabled={step === 0} onClick={() => setStep(step - 1)}>Back</button>
              <button type="button" onClick={() => step + 1 === tour.length ? finish() : setStep(step + 1)}>
                {step + 1 === tour.length ? 'Finish' : 'Next'}
              </button>
              <button type="button" onClick={finish}>Skip</button>
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
              <button ref={trigger} type="button" onClick={() => setStep(0)} hidden={Boolean(active)}>Start tour</button>
            </ReportsHeading>
            <ReportTimeProvider capturedAt={capturedAt}>
              <ReportsLayout sidebar={queuedAsks && Object.values(queuedAsks).some(rows => rows.length > 0)
                ? <QueuedAsksContent snapshot={queuedAsks} /> : undefined}>
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

createRoot(document.getElementById('root')).render(<SummaryDemo />)
