import React, { createContext, useContext, useLayoutEffect } from 'react'
import { legacyLocation } from './boardRoutes.js'

export const BoardLocation = createContext(null)
export const ContentTitle = createContext(null)

export function useBoardLocation() {
  return useContext(BoardLocation)
}

export function useBoardNavigation() {
  return useBoardLocation()?.navigate
}

export function useContentTitle(title) {
  const setTitle = useContext(ContentTitle)
  useLayoutEffect(() => {
    if (!title || !setTitle) return
    setTitle(title)
    return () => setTitle(null)
  }, [title, setTitle])
}

export function BoardLink(props) {
  const { href, ...attributes } = props
  const board = useBoardLocation()
  if (board && href?.startsWith('#')) {
    const to = legacyLocation(href) || {
      pathname: board.location.pathname,
      search: board.location.search,
      hash: href,
    }
    const Link = board.link
    const ticket = /^#task-/.test(href) ? href.split('?', 1)[0] : undefined
    return <Link to={to} data-ticket-fragment={ticket} {...attributes} />
  }
  return <a {...props} />
}
