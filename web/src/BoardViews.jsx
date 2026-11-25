import React, { useEffect, useRef } from 'react'
import { DemoUnavailable } from './DemoControl.jsx'
import { projectHref } from './projectScope.js'

export default function BoardViews({ current, reportsHref, projectScope, isAgent, showNotifications, showKnowledge, preview = false }) {
  const nav = useRef(null)
  useEffect(() => {
    const strip = nav.current
    function revealActive() {
      if (strip.scrollWidth <= strip.clientWidth) return
      const activeLink = strip.querySelector('[aria-current="page"]')
      if (!activeLink) return

      // Reveal a clipped active tab without scrolling the page away from the route's heading.
      const { left, right } = activeLink.getBoundingClientRect()
      const bounds = strip.getBoundingClientRect()
      if (left < bounds.left) strip.scrollLeft += left - bounds.left
      else if (right > bounds.right) strip.scrollLeft += right - bounds.right
    }

    revealActive()
    window.addEventListener('resize', revealActive)
    return () => window.removeEventListener('resize', revealActive)
  }, [current, isAgent, showNotifications, showKnowledge])

  const links = [
    ['reports', 'Reports'],
    ['board', 'Tasks'],
    ['activity', 'Activity'],
    ...(showKnowledge ? [['knowledge', 'Knowledge']] : []),
    ['stats', 'Stats'],
    ...(showNotifications ? [['notifications', 'Notifications']] : []),
    ...(isAgent ? [['my-activity', 'My activity']] : []),
  ]
  return (
    <nav ref={nav} className="board-views" aria-label="Taskboard views">
      {links.map(([id, label]) => (
        preview && id !== current ? <DemoUnavailable key={id} className="offline-view">{label}</DemoUnavailable>
          : <a key={id} href={id === 'reports' ? reportsHref : projectHref(`#${id}`, projectScope)}
            aria-current={current === id ? 'page' : undefined}>{label}</a>
      ))}
    </nav>
  )
}
