import React, { useId, useRef, useState } from 'react'
import { DemoControlDescription } from '../../src/DemoControl.jsx'

const notice = 'This is a demo. Some Taskboard features are unavailable here.'

function control(target) {
  return target instanceof Element ? target.closest('[data-demo-control]:not(:disabled)') : null
}

export default function DemoNotice({ children }) {
  const description = useId()
  const title = useId()
  const message = useId()
  const dialog = useRef(null)
  const invoker = useRef(null)
  const pointer = useRef(null)
  const [tooltip, setTooltip] = useState(null)
  const [destination, setDestination] = useState(null)

  function showTooltip(element) {
    const bounds = element.getBoundingClientRect()
    setTooltip({
      '--demo-notice-left': `${bounds.left}px`,
      ...(bounds.bottom + 90 < window.innerHeight
        ? { top: bounds.bottom + 6 }
        : { bottom: window.innerHeight - bounds.top + 6 }),
    })
  }

  function activate(event) {
    const element = control(event.target)
    // WebKit can send a mouse click after a touch pointerdown.
    const touch = event.nativeEvent.pointerType === 'touch' || (event.detail > 0 && pointer.current === 'touch')
    pointer.current = null
    if (!element) {
      setTooltip(null)
      return
    }
    const externalLink = element instanceof HTMLAnchorElement && element.origin !== window.location.origin
    if (element.hasAttribute('data-demo-unavailable') || (touch && externalLink)) {
      event.preventDefault()
      invoker.current = element
      setDestination(touch && externalLink ? element.href : null)
      setTooltip(null)
      dialog.current.showModal()
    } else if (touch) {
      showTooltip(element)
    }
  }

  return (
    <DemoControlDescription.Provider value={description}>
      <div onFocus={event => {
        const element = control(event.target)
        if (element) showTooltip(element)
      }}
      onBlur={event => {
        if (control(event.target)) setTooltip(null)
      }}
      onPointerOver={event => {
        const element = control(event.target)
        if (element && event.pointerType !== 'touch' && element !== control(event.relatedTarget)) {
          showTooltip(element)
        }
      }}
      onPointerOut={event => {
        const element = control(event.target)
        if (element && event.pointerType !== 'touch' && element !== control(event.relatedTarget)) {
          setTooltip(null)
        }
      }}
      onPointerDownCapture={event => { pointer.current = event.pointerType }}
      onClickCapture={activate}
      onKeyDownCapture={event => {
        if (event.key === 'Escape' && tooltip) {
          event.stopPropagation()
          setTooltip(null)
        }
      }}>
        {children}
      </div>
      <div id={description} className="demo-tooltip" role="tooltip" hidden={!tooltip} style={tooltip ?? undefined}>
        {notice}
      </div>
      <dialog ref={dialog} className="demo-dialog" aria-labelledby={title} aria-describedby={message}
        onClose={() => {
          invoker.current?.focus({ preventScroll: true })
          setTooltip(null)
        }}>
        <h2 id={title}>Taskboard demo</h2>
        <p id={message}>{notice} {destination ? 'This link opens GitHub.' : 'This action requires the full board.'}</p>
        <div className="demo-dialog-actions">
          <button type="button" aria-describedby={message} onClick={() => dialog.current.close()}>Close</button>
          {destination && <a href={destination} aria-describedby={message}>Continue to GitHub</a>}
        </div>
      </dialog>
    </DemoControlDescription.Provider>
  )
}
