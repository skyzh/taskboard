;(async function () {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
  const rect = node => node.getBoundingClientRect().toJSON()
  const canvas = document.createElement('canvas').getContext('2d')

  function measure(label) {
    const node = [...label.childNodes].find(child => child.nodeType === Node.TEXT_NODE && child.textContent.trim())
    const range = document.createRange()
    range.selectNode(node)
    const textBox = rect(range)
    const style = getComputedStyle(label)
    const { fontFamily, fontSize, fontWeight, lineHeight, color } = style
    const sampleWidths = Object.fromEntries([fontFamily, '-apple-system', 'sans-serif'].map(family => {
      canvas.font = `${fontWeight} 72px ${family}`
      return [family, canvas.measureText(label.textContent.trim()).width]
    }))
    const marker = document.createElement('span')
    marker.style.cssText = 'display:inline-block;width:0;height:0;overflow:hidden;vertical-align:baseline'
    const wrapper = document.createElement('span')
    node.replaceWith(wrapper)
    wrapper.append(node, marker)
    range.selectNode(node)
    const measuredBox = rect(range)
    const baseline = marker.getBoundingClientRect().top
    wrapper.replaceWith(node)
    return {
      text: label.textContent.trim(), baseline, textBox, box: rect(label),
      fontFamily, fontSize, fontWeight, lineHeight, color, sampleWidths,
      textBoxUnchanged: ['top', 'left', 'width', 'height'].every(key => Math.abs(textBox[key] - measuredBox[key]) < .02),
    }
  }

  try {
    let row, statusLabel, priorityLabel
    for (let attempt = 0; attempt < 150; attempt += 1) {
      row = document.querySelector('.task-detail .detail-meta:has(.task-priority)')
      statusLabel = row?.querySelector('.status')
      priorityLabel = row?.querySelector('.task-priority')
      if (statusLabel?.textContent.trim() === 'In progress' &&
          priorityLabel?.textContent.trim() === 'Priority Normal') break
      await sleep(200)
    }
    if (statusLabel?.textContent.trim() !== 'In progress' ||
        priorityLabel?.textContent.trim() !== 'Priority Normal') {
      throw Error('Synthetic status and priority labels did not render')
    }
    await document.fonts.ready
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
    const status = measure(statusLabel)
    const priority = measure(priorityLabel)
    const data = {
      title: document.querySelector('.task-detail h1, .task-detail h2')?.textContent.trim(),
      ua: navigator.userAgent,
      viewport: { width: innerWidth, height: innerHeight, scale: visualViewport.scale, dpr: devicePixelRatio },
      fonts: document.fonts.status,
      row: { ...rect(row), alignItems: getComputedStyle(row).alignItems },
      status, priority,
      baselineDelta: +(status.baseline - priority.baseline).toFixed(3),
    }
    await fetch('/tasks/__measure', { method: 'POST', body: JSON.stringify(data, null, 2) })
  } catch (error) {
    await fetch('/tasks/__measure', { method: 'POST', body: JSON.stringify({ error: String(error), ua: navigator.userAgent }) })
  }
})()
