import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import { ReportTimeProvider } from './ReportTime.jsx'
import MilestoneMap from './MilestoneMap.jsx'
import './styles.css'
import './night.css'

createRoot(document.getElementById('root')).render(
  <ReportTimeProvider><App MilestoneMap={MilestoneMap} /></ReportTimeProvider>,
)
