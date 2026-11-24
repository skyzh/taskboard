import React, { useLayoutEffect, useRef } from 'react'

export default function BoardTour({ step, index, total, onMove, onClose }) {
  const panel = useRef(null)

  useLayoutEffect(() => {
    panel.current.focus({ preventScroll: true })
    const target = document.getElementById(step.target)
    if (!target) return
    target.dataset.boardTourTarget = ''
    target.scrollIntoView({ block: 'nearest' })
    return () => { delete target.dataset.boardTourTarget }
  }, [step])

  const last = index === total - 1
  return (
    <aside className="board-tour" ref={panel} tabIndex={-1} aria-labelledby="board-tour-title"
      onKeyDown={event => { if (event.key === 'Escape') onClose() }}>
      <h2 id="board-tour-title">{step.title} <span>{index + 1} of {total}</span></h2>
      <p>{step.text}</p>
      <div className="board-tour-actions">
        <button type="button" disabled={index === 0} onClick={() => onMove(index - 1)}>Back</button>
        <button type="button" onClick={last ? onClose : () => onMove(index + 1)}>{last ? 'Finish' : 'Next'}</button>
        {!last && <button type="button" onClick={onClose}>Skip</button>}
      </div>
    </aside>
  )
}
