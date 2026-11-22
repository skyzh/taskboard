import React, { createContext, useContext } from 'react'

export const DemoControlDescription = createContext(undefined)

export function DemoControl({ as: Element, ...props }) {
  const description = useContext(DemoControlDescription)
  return <Element {...props} aria-describedby={description} data-demo-control={description ? '' : undefined} />
}

export function DemoUnavailable({ as: Fallback = 'span', className, children }) {
  const description = useContext(DemoControlDescription)
  return description
    ? <DemoControl as="button" type="button" className={className} aria-haspopup="dialog" data-demo-unavailable="">
      {children}
    </DemoControl>
    : <Fallback className={className}>{children}</Fallback>
}
