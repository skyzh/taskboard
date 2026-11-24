import React from 'react'
import { createRoot } from 'react-dom/client'
import App from '../../src/App.jsx'
import { configurePreviewData } from '../../src/data.js'
import { ReportTimeProvider } from '../../src/ReportTime.jsx'
import { capturedAt, previewRequest } from './fixture.js'
import '../../src/styles.css'
import '../../src/night.css'

const steps = [
  { route: 'reports', target: '.report-owner-heading', title: 'Start with Raft agents',
    text: 'This replay follows Taskboard PR #234. Prism owned the Reports change. Gauge took the independent review. Both are Raft agents enrolled as Team DRIs, the people or agents responsible for the work.' },
  { route: 'task-T-1406', target: '.detail-brief', title: 'One ticket carries the work',
    text: 'Prism took the request to open All projects from the home URL. The ticket preserves the requested behavior and links the implementation, review fix, and checks.' },
  { route: 'reports', target: '.request-log', title: 'Hand the review to Gauge',
    text: 'Prism asked Gauge for an independent review. The Request log links the review back to the ticket and shows the completed handoff.' },
  { route: 'event-2-log', target: '#event-2-log', title: 'See what stopped the release',
    text: 'Gauge ran the tests and found that one still expected the old home page behavior. The linked Activity entry records the finding so Prism could pick up the fix.' },
  { route: 'activity?actor=prism&detail=all', target: '#event-3-log', title: 'Follow the test fix',
    text: 'Prism fixed the assertion and tested the bare home URL separately. The Activity link on Prism’s report opens this filter. You can follow a Team DRI’s work from your own browser session.' },
  { route: 'activity?actor=gauge&detail=all', target: '#event-4-log', title: 'See the handoff return',
    text: 'Gauge checked the new revision and approved it, returning the ticket to Prism to ship. Activity on Gauge’s report shows the review from the same human view.' },
  { route: 'event-5-log', target: '#event-5-log', title: 'Read the verified result',
    text: 'Prism shipped the approved revision. The released result leads back to the ticket and the passing protected main checks.' },
  { route: 'integrations?project=T', target: '.integration-identity', title: 'Report from your own agent session',
    text: 'A personal coding session can publish useful progress and reuse Knowledge. Leave the reporting identity on Human to report under your profile when that session runs.' },
  { route: 'integrations?project=T', target: '.integration-paths li:nth-child(4) button', title: 'Hand work to a Local DRI',
    text: 'Select Local DRI to see how to register a named recipient for handoffs. Its watcher accepts Ready asks from the approved requester and runs a worker on your machine. Raft Team DRIs already enroll when they connect.' },
]

const notice = <>Read-only demo of <a href="https://github.com/skyzh/taskboard/pull/234">Taskboard PR #234</a>.</>
configurePreviewData(previewRequest)
createRoot(document.getElementById('root')).render(
  <ReportTimeProvider capturedAt={capturedAt}><App previewNotice={notice} tourSteps={steps} /></ReportTimeProvider>,
)
