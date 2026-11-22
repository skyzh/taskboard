import React, { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { AssigneeReport } from '../../src/Reports.jsx'
import { capturedAt, entries } from './snapshot.js'
import '../../src/styles.css'
import './snapshot.css'

const tour = [
  { id: 'snapshot-current', title: 'Current work', text: 'Read the next step and status on a ticket that is still open.' },
  { id: 'snapshot-goals', title: 'Goals', text: 'See what each person is responsible for improving.' },
  { id: 'snapshot-outcomes', title: 'Recent outcomes', text: 'Follow each result to the pull request where it was reviewed.' },
]

const tourTargets = { current: tour[0].id, goals: tour[1].id, outcomes: tour[2].id }

function SummaryDemo() {
  const [step, setStep] = useState(null)
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
    target?.scrollIntoView({
      block: 'center',
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',
    })
  }, [active])

  function finish() {
    restoreFocus.current = true
    setStep(null)
  }

  return (
    <main className="main-content snapshot" onKeyDown={event => {
      if (event.key === 'Escape' && active) finish()
    }}>
      <header className="page-heading snapshot-heading">
        <div>
          <h1>Taskboard Summary</h1>
          <p>Public snapshot from <time dateTime={capturedAt}>November 22, 2026 UTC</time>. Goals reflect published roles.</p>
        </div>
        <button ref={trigger} type="button" onClick={() => setStep(0)} hidden={Boolean(active)}>Start tour</button>
      </header>
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
      <ol className="report-list" aria-label="Taskboard squad Summary" role="list">
        {entries.map(entry => (
          <AssigneeReport key={entry.owner} entry={entry} publicPreview
            tourTargets={entry.owner === 'prism' ? tourTargets : undefined} activeTarget={active?.id} />
        ))}
      </ol>
    </main>
  )
}

createRoot(document.getElementById('root')).render(<SummaryDemo />)
