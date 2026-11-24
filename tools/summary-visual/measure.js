function sampleRows() {
  function bounds(element) {
    const {left, top, right, bottom} = element.getBoundingClientRect()
    return {left, top, right, bottom}
  }
  const tasks = [...document.querySelectorAll('.report-task-summary.report-current-work')].map(row => {
    const status = row.querySelector('.report-work-status')
    const label = status.querySelector('.status')
    const heading = row.querySelector('.task-row-heading')
    const title = heading.querySelector(':scope > a')
    const step = row.querySelector('.report-task-cue')
    const dependency = row.querySelector('.task-dependency')
    const text = document.createRange()
    text.selectNodeContents(title)
    const titleTop = title.getBoundingClientRect().top
    const lineHeight = parseFloat(getComputedStyle(title).lineHeight)
    const titleLines = new Set([...text.getClientRects()].filter(rect => rect.width > 0)
      .map(rect => Math.round((rect.top - titleTop) / lineHeight))).size
    return {
      key: title.querySelector('.ticket-id').textContent, state: label.textContent,
      visibleStatus: label.getBoundingClientRect().width > 0 && getComputedStyle(label).visibility === 'visible',
      heading: bounds(heading), status: bounds(status), step: bounds(step), titleLines,
      dependency: dependency && {text: dependency.textContent, bounds: bounds(dependency)},
    }
  }).sort((a, b) => a.key.localeCompare(b.key))
  return {
    viewport: {width: innerWidth, height: innerHeight, scale: visualViewport.scale, dpr: devicePixelRatio},
    ua: navigator.userAgent, tasks,
    overflow: document.documentElement.scrollWidth > innerWidth,
  }
}
