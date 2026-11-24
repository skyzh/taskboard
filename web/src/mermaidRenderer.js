import engine from '@mermaid-js/tiny/dist/mermaid.tiny.js?url'
import bootstrap from './mermaidFrame.js?url&no-inline'

let ready
let nextId = 0

function rendererFrame() {
  ready ??= new Promise(resolve => {
    const frame = document.createElement('iframe')
    const scripts = [engine, bootstrap].map(path => new URL(path, document.baseURI))
    const scriptSources = scripts.map(url => url.origin + url.pathname).join(' ')
    // The opaque origin keeps diagram rendering out of the board document. The
    // frame can load only its two local scripts and cannot make diagram requests.
    frame.setAttribute('sandbox', 'allow-scripts')
    frame.setAttribute('aria-hidden', 'true')
    frame.tabIndex = -1
    frame.className = 'mermaid-renderer-frame'
    frame.srcdoc = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; script-src ${scriptSources}">` +
      scripts.map(url => `<script src="${url.href}"></script>`).join('')
    frame.addEventListener('load', () => resolve(frame.contentWindow), { once: true })
    document.body.append(frame)
  })
  return ready
}

export async function renderMermaid(text) {
  const frame = await rendererFrame()
  const id = ++nextId
  const { svg, alt, width } = await new Promise((resolve, reject) => {
    function finish(value) {
      window.clearTimeout(timeout)
      window.removeEventListener('message', receive)
      if (typeof value?.svg === 'string') resolve(value)
      else reject(new Error('The diagram could not be rendered.'))
    }
    function receive(event) {
      if (event.source === frame && event.data?.id === id) finish(event.data)
    }
    const timeout = window.setTimeout(finish, 8000)
    window.addEventListener('message', receive)
    frame.postMessage({ id, text }, '*')
  })
  return {
    src: `data:image/svg+xml,${encodeURIComponent(svg)}`,
    alt: alt || 'Mermaid diagram',
    width: Number.isFinite(width) && width > 0 ? Math.ceil(width) : undefined,
  }
}
