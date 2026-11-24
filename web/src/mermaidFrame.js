/* Runs as a classic script inside the sandboxed renderer frame. */
const mermaid = window.mermaid
mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', htmlLabels: false,
  suppressErrorRendering: true, theme: 'neutral' })

window.addEventListener('message', async event => {
  const { id, text } = event.data || {}
  if (event.source !== window.parent || !Number.isInteger(id) || typeof text !== 'string') return
  try {
    const { svg } = await mermaid.render(`diagram-${id}`, text)
    const template = document.createElement('template')
    template.innerHTML = svg
    const image = template.content.querySelector('svg')
    const width = Number(image.getAttribute('viewBox')?.trim().split(/\s+/)[2])
    event.source.postMessage({ id, svg: new XMLSerializer().serializeToString(image),
      alt: image.querySelector('title')?.textContent, width }, event.origin)
  } catch {
    event.source.postMessage({ id }, event.origin)
  }
})
