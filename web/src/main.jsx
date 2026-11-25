import React from 'react'
import { createRoot } from 'react-dom/client'
import BoardRouter from './BoardRouter.jsx'
import { ReportTimeProvider } from './ReportTime.jsx'
import './styles.css'
import './night.css'

// Keep the mount fixed after history navigation changes the document URL.
const base = document.querySelector('base')
base.href = new URL(document.baseURI).pathname

createRoot(document.getElementById('root')).render(
  <ReportTimeProvider><BoardRouter /></ReportTimeProvider>,
)
