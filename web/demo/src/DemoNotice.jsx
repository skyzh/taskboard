import React, { useId, useRef } from 'react'
import { DemoUnavailableAction } from '../../src/DemoControl.jsx'

export default function DemoNotice({ children }) {
  const title = useId()
  const message = useId()
  const dialog = useRef(null)
  const invoker = useRef(null)

  function showUnavailable(button) {
    invoker.current = button
    dialog.current.showModal()
  }

  return (
    <DemoUnavailableAction.Provider value={showUnavailable}>
      {children}
      <dialog ref={dialog} className="demo-dialog" aria-labelledby={title} aria-describedby={message}
        onClose={() => invoker.current?.focus({ preventScroll: true })}>
        <h2 id={title}>Taskboard demo</h2>
        <p id={message}>This action requires the full board.</p>
        <button className="demo-dialog-close" type="button" onClick={() => dialog.current.close()}>Close</button>
      </dialog>
    </DemoUnavailableAction.Provider>
  )
}
