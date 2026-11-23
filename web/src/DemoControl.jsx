import React, { createContext, useContext } from 'react'

export const DemoUnavailableAction = createContext(null)

export function DemoUnavailable({ as: Fallback = 'span', className, children }) {
  const showUnavailable = useContext(DemoUnavailableAction)
  return showUnavailable
    ? <button type="button" className={className} aria-haspopup="dialog" data-demo-unavailable=""
        onClick={event => showUnavailable(event.currentTarget)}>
      {children}
    </button>
    : <Fallback className={className}>{children}</Fallback>
}
