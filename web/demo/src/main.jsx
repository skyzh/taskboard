import React from 'react'
import { createRoot } from 'react-dom/client'
import App from '../../src/App.jsx'
import { configurePreviewData } from '../../src/data.js'
import { ReportTimeProvider } from '../../src/ReportTime.jsx'
import { AssigneeAvatar } from '../../src/shared.jsx'
import { capturedAt, previewRequest, profiles } from './fixture.js'
import '../../src/styles.css'
import '../../src/night.css'

function Agent({ id }) {
  const { name, avatar_url: avatar } = profiles[id]
  return <span className="demo-tour-agent"><AssigneeAvatar name={name} url={avatar} />{name}</span>
}

const activityRoute = 'activity?detail=all'
const steps = [
  { route: 'reports', target: '.report-owner-heading', title: 'Start with Raft agents',
    text: <>This replay follows Taskboard PR #234. <Agent id="prism" /> owned the Reports change.{' '}
      <Agent id="gauge" /> took the independent review. Both are Raft agents enrolled as Team DRIs,
      the people or agents responsible for the work.</> },
  { route: 'reports', target: '.request-log', title: 'Find the review request',
    text: <><Agent id="prism" /> asked <Agent id="gauge" /> for an independent review. The Request log
      links the review to the ticket and shows the completed handoff.</> },
  { route: 'task-T-1406', target: '.detail-brief', title: 'Read the ticket',
    text: <><Agent id="prism" /> took the request to open All projects from the home URL. The ticket preserves
      the requested behavior and links the implementation, review fix, and checks.</> },
  { route: activityRoute, target: '#event-2-log', title: 'See what stopped the release',
    text: <><Agent id="gauge" /> found a test that still expected the old home page behavior. The Activity
      entry records the finding so <Agent id="prism" /> could pick up the fix.</> },
  { route: activityRoute, target: '#event-3-log', title: 'Follow the test fix',
    text: <><Agent id="prism" /> fixed the assertion and tested the bare home URL separately. To follow
      one Team DRI’s work, open Activity on their report.</> },
  { route: activityRoute, target: '#event-4-log', title: 'See the handoff return',
    text: <><Agent id="gauge" /> checked the new revision and approved it, returning the ticket
      to <Agent id="prism" /> to ship.</> },
  { route: activityRoute, target: '#event-5-log', title: 'Read the verified result',
    text: <><Agent id="prism" /> shipped the approved revision. The result leads back to the ticket
      and the passing protected main checks.</> },
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
