import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import MilestoneMap from './MilestoneMap.jsx'
import './styles.css'
import './night.css'

createRoot(document.getElementById('root')).render(<App MilestoneMap={MilestoneMap} />)
