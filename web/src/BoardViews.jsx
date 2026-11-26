import React, { useEffect, useRef } from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import { DemoUnavailable } from './DemoControl.jsx'
import { projectHref } from './projectScope.js'

export default function BoardViews({ current, reportsHref, projectScope, isAgent, isHuman, showNotifications, showKnowledge, preview = false }) {
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
    ...(showNotifications ? [['notifications', 'Notifications', isHuman ? 'notifications-log' : 'notifications']] : []),
    ...(isAgent ? [['my-activity', 'My activity']] : []),
  ]
  return (
    <nav ref={nav} className="board-views" aria-label="Taskboard views">
      {links.map(([id, label, route = id]) => (
        preview && id !== current ? <DemoUnavailable key={id} className="offline-view">{label}</DemoUnavailable>
          : <BoardLink key={id} href={id === 'reports' ? reportsHref : projectHref(`#${route}`, projectScope)}
            aria-current={current === id ? 'page' : undefined}>{label}</BoardLink>
      ))}
    </nav>
  )
}
