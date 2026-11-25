import React, { useCallback, useLayoutEffect, useState } from 'react'
import {
  BrowserRouter, HashRouter, Link, Navigate, useLocation, useNavigate, useNavigationType, useParams, useRoutes,
} from 'react-router'
import App from './App.jsx'
import { boardRoutes, legacyLocation, pageFragment, routeAddress } from './boardRoutes.js'

import { BoardLocation, ContentTitle } from './BoardNavigation.jsx'

const unknown = { id: 'not-found', title: 'Page unavailable' }

function BoardPage({ route, preview, ...props }) {
  const location = useLocation()
  const params = useParams()
  const move = useNavigate()
  const navigationType = useNavigationType()
  const legacy = !preview && route.id === 'home' && location.hash && legacyLocation(location.hash)
  const rawFragment = pageFragment(route.id, location, params)
  const [heading, setHeading] = useState(null)
  const page = location.pathname
  const setTitle = useCallback(title => setHeading({ page, title }), [page])
  const appName = preview ? 'Taskboard Demo' : 'Taskboard'
  const title = heading?.page === page && heading.title ? heading.title : route.title
  useLayoutEffect(() => {
    document.title = `${title} — ${appName}`
  }, [title, appName])
  const navigate = useCallback((href, { replace = false, reveal = true } = {}) => {
    const next = legacyLocation(href)
    if (next && routeAddress(next) !== routeAddress(location)) {
      move(next, { replace, state: reveal ? null : { reveal: false } })
    }
  }, [location, move])
  if (legacy) {
    const filters = new URLSearchParams(location.search)
    for (const [key, value] of new URLSearchParams(legacy.search)) filters.set(key, value)
    const search = filters.size ? '?' + filters : ''
    return <Navigate to={{ ...legacy, search }} replace />
  }
  return <BoardLocation.Provider value={{
    route: route.id, rawFragment, location, navigate, link: Link, reveal: navigationType === 'POP' || location.state?.reveal !== false,
  }}>
    <ContentTitle.Provider value={setTitle}><App {...props} /></ContentTitle.Provider>
  </BoardLocation.Provider>
}

function BoardPages(props) {
  return useRoutes([
    ...boardRoutes.map(route => ({ path: route.path, element: <BoardPage route={route} {...props} /> })),
    { path: '*', element: <BoardPage route={unknown} {...props} /> },
  ])
}

export default function BoardRouter(props) {
  if (props.previewNotice) return <HashRouter><BoardPages preview {...props} /></HashRouter>
  const basename = new URL(document.baseURI).pathname
  return <BrowserRouter basename={basename}><BoardPages {...props} /></BrowserRouter>
}

// A static host serves the demo document once. Redirect old demo fragments in
// place before HashRouter starts watching the browser's history.
export function migrateDemoLink() {
  const hash = window.location.hash.slice(1)
  const legacy = hash && !hash.startsWith('/') && legacyLocation(hash)
  if (legacy) window.history.replaceState(window.history.state, '', '#' + routeAddress(legacy))
}
