function sampleRows() {
  function baseline(label) {
    const range = document.createRange()
    range.selectNodeContents(label)
    const before = range.getBoundingClientRect()
    const marker = document.createElement('span')
    marker.style.cssText = 'display:inline-block;width:0;height:0;overflow:hidden;vertical-align:baseline'
    label.prepend(marker)
    range.selectNodeContents(label)
    const after = range.getBoundingClientRect()
    const value = marker.getBoundingClientRect().top
    marker.remove()
    const unchanged = ['x', 'y', 'width', 'height'].every(key => Math.abs(before[key] - after[key]) < .02)
    return {value, unchanged}
  }
  const round = number => +number.toFixed(3)
  const tasks = [...document.querySelectorAll('.report-task-summary .task-row-heading > .report-status')].map(status => {
    const heading = status.parentElement
    const title = heading.querySelector(':scope > a')
    const id = title.querySelector('.ticket-id')
    const label = status.firstElementChild
    const dot = getComputedStyle(status, '::before')
    const style = getComputedStyle(label)
    const visuallyHidden = style.position === 'absolute'
    const titleBaseline = baseline(id)
    const statusBaseline = visuallyHidden ? null : baseline(label)
    const statusBox = status.getBoundingClientRect()
    return {
      key: id.textContent, label: label.textContent, visuallyHidden,
      statusBaseline, titleBaseline, gap: statusBaseline && round(statusBaseline.value - titleBaseline.value),
      titleLines: title.getClientRects().length,
      dot: {width: dot.width, height: dot.height, color: dot.backgroundColor},
      compactDotCenter: visuallyHidden ? round(statusBox.y + statusBox.height / 2 - titleBaseline.value) : null,
      priority: heading.querySelector('.task-priority')?.textContent || null,
    }
  }).sort((a, b) => a.key.localeCompare(b.key))
  return {
    viewport: {width: innerWidth, height: innerHeight, scale: visualViewport.scale, dpr: devicePixelRatio},
    ua: navigator.userAgent, tasks,
    waiting: document.querySelector('.report-waiting-line')?.textContent.trim() || null,
    overflow: document.documentElement.scrollWidth > innerWidth,
  }
}
