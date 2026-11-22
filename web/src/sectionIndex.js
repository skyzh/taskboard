export function watchBoardSections(nav, onChange) {
  const header = nav.closest('header')
  const main = document.querySelector('main')
  const headings = Array.from(nav.querySelectorAll('a'), link => document.getElementById(link.hash.slice(1)))
    .filter(Boolean)
  if (!headings.length) return

  const scrollport = document.scrollingElement
  let frame = null
  let current

  function measure() {
    const scrollMargin = Number.parseFloat(window.getComputedStyle(headings[0]).scrollMarginTop) || 0
    const end = scrollport.scrollHeight - scrollport.clientHeight
    // A short final section may never reach the activation line.
    const atBottom = end > 1 && scrollport.scrollTop >= end - 1
    const line = atBottom
      ? scrollport.clientHeight
      : header.getBoundingClientRect().bottom + scrollMargin + 1
    let next
    for (const heading of headings) {
      const bounds = heading.getBoundingClientRect()
      if (!bounds.height) continue
      next ??= heading.id
      if (bounds.top < line) next = heading.id
    }
    if (next !== current) {
      current = next
      onChange(next)
    }
  }

  function schedule() {
    if (frame !== null) return
    frame = window.requestAnimationFrame(() => {
      frame = null
      measure()
    })
  }

  measure()
  window.addEventListener('scroll', schedule, { passive: true })
  window.addEventListener('resize', schedule)
  const resize = new ResizeObserver(schedule)
  resize.observe(header)
  resize.observe(main)
  const content = main.querySelector('.main-content')
  if (content) resize.observe(content)
  for (const heading of headings) resize.observe(heading.closest('section') || heading)

  return () => {
    window.removeEventListener('scroll', schedule)
    window.removeEventListener('resize', schedule)
    resize.disconnect()
    if (frame !== null) window.cancelAnimationFrame(frame)
  }
}
