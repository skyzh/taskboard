import React, { useEffect, useState } from 'react'

export default function MermaidDiagram({ text }) {
  const [diagram, setDiagram] = useState(null)

  useEffect(() => {
    let current = true
    import('./mermaidRenderer.js').then(({ renderMermaid }) => renderMermaid(text)).then(
      image => { if (current) setDiagram(image) },
      () => {},
    )
    return () => { current = false }
  }, [text])

  const source = <pre><code>{text}</code></pre>
  if (!diagram) return source
  return <div className="mermaid-diagram">
    <div className="mermaid-viewport" role="region" aria-label={diagram.alt} tabIndex={0}>
      <img src={diagram.src} alt={diagram.alt} width={diagram.width} onError={() => setDiagram(null)} />
    </div>
    <details><summary>Diagram source</summary>{source}</details>
  </div>
}
