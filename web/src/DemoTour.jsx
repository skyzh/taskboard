import React, { useLayoutEffect, useRef } from 'react'

export function withoutTour(fragment) {
  const [route, query] = fragment.split('?', 2)
  const params = new URLSearchParams(query)
  if (!params.has('tour')) return fragment
  params.delete('tour')
  return params.size ? `${route}?${params}` : route
}

function matchesStep(fragment, route) {
  const base = fragment.split('?', 1)[0]
  if (route === 'reports') return ['', 'reports', 'reports-all'].includes(base)
  return fragment === route
}

function tourFragment(route, number) {
  return `${route}${route.includes('?') ? '&' : '?'}tour=${number}`
}

export function useDemoTour(rawFragment, steps = []) {
  const [, query] = rawFragment.split('?', 2)
  const value = new URLSearchParams(query).get('tour')
  const number = /^\d+$/.test(value || '') ? Number(value) : -1
  const step = steps[number - 1]
  const active = steps.length > 0 && (number === 0 || Boolean(step))
  const toggleButton = useRef(null)
  const panel = useRef(null)
  const restore = useRef(false)
  const launcher = useRef(null)

  function open(index = 0) {
    window.location.hash = tourFragment(steps[index].route, index + 1)
  }

  function stop() {
    restore.current = true
    window.location.hash = withoutTour(rawFragment)
  }

  function toggle() {
    if (active) {
      stop()
    } else {
      launcher.current = toggleButton.current
      const route = withoutTour(rawFragment)
      const index = steps.findIndex(item => matchesStep(route, item.route))
      window.location.hash = tourFragment(route, index + 1)
    }
  }

  useLayoutEffect(() => {
    if (active) panel.current?.focus({ preventScroll: true })
    else if (restore.current) {
      restore.current = false
      const target = launcher.current?.isConnected ? launcher.current : toggleButton.current
      target?.focus({ preventScroll: true })
    }
  }, [active, number])

  useLayoutEffect(() => {
    if (!step) return
    let outlined
    let frame
    function highlight() {
      const target = document.querySelector(step.target)
      if (!target || target === outlined) return
      outlined?.removeAttribute('data-tour-target')
      outlined = target
      target.setAttribute('data-tour-target', '')
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => target.scrollIntoView({ block: 'nearest', inline: 'nearest' }))
    }
    const main = document.querySelector('main')
    const observer = new MutationObserver(highlight)
    if (main) observer.observe(main, { childList: true, subtree: true })
    highlight()
    return () => {
      observer.disconnect()
      cancelAnimationFrame(frame)
      outlined?.removeAttribute('data-tour-target')
    }
  }, [step])

  return {
    toggle: steps.length > 0 && <button className="navbar-tour-trigger demo-tour-toggle" type="button" ref={toggleButton} aria-pressed={active}
      onClick={toggle}>Tour</button>,
    start: event => {
      launcher.current = event.currentTarget
      const route = withoutTour(rawFragment)
      if (matchesStep(route, steps[0].route)) window.location.hash = tourFragment(route, 1)
      else open()
    },
    panel: active && <aside className="board-tour demo-tour" aria-labelledby="tour-title" ref={panel} tabIndex={-1}
      onKeyDown={event => { if (event.key === 'Escape') stop() }}>
      <h2 id="tour-title">{step?.title || 'Take the tour'}{step && <span> {number} of {steps.length}</span>}</h2>
      <p>{step?.text || 'Follow the walkthrough from Reports, or turn off the tour to keep working here.'}</p>
      <div className="board-tour-actions">
        {step && number > 1 && <button type="button" onClick={() => open(number - 2)}>Back</button>}
        {number < steps.length && <button type="button" onClick={() => open(number)}>{step ? 'Next' : 'Start tour'}</button>}
        <button type="button" onClick={stop}>{number === steps.length ? 'Finish' : 'Close tour'}</button>
      </div>
    </aside>,
  }
}
