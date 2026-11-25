import React from 'react'
import { BoardLink } from './BoardNavigation.jsx'
import MarkdownIt from 'markdown-it'
import MermaidDiagram from './MermaidDiagram.jsx'
import { markdownHref, sourceLinkLabel } from './sourceLinks.js'

const markdown = new MarkdownIt({ html: false, linkify: true })
const ticketReference = /(?<![\p{L}\p{N}_/-])([A-Za-z][A-Za-z0-9]{0,7})-([0-9]{1,19})(?![\p{L}\p{N}_/-])/gu
const elements = new Set([
  'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'ul', 'ol', 'li',
  'em', 'strong', 's', 'thead', 'tbody', 'tr', 'th', 'td',
])

function linkedTickets(text, related, tokenIndex) {
  const parts = []
  let end = 0
  for (const match of text.matchAll(ticketReference)) {
    const key = match[1].toUpperCase() + '-' + match[2].replace(/^0+/, '')
    const href = markdownHref(related.get(key)?.url)
    if (!href) continue
    parts.push(text.slice(end, match.index), <BoardLink key={`${tokenIndex}-${match.index}`} href={href}>{match[0]}</BoardLink>)
    end = match.index + match[0].length
  }
  parts.push(text.slice(end))
  return parts
}

function tokensToElements(tokens, links, related) {
  const frames = [{ children: [], inLink: false }]
  for (const [index, token] of tokens.entries()) {
    const current = frames.at(-1)
    if (token.nesting === 1) {
      frames.push({ token, index, children: [], inLink: current.inLink || token.tag === 'a' })
    } else if (token.nesting === -1) {
      const opened = frames.pop()
      const parent = frames.at(-1).children
      const { tag, hidden } = opened.token
      if (tag === 'a') {
        const target = opened.token.attrGet('href')
        const href = links && markdownHref(target)
        const automatic = ['autolink', 'linkify'].includes(opened.token.markup)
        if (href) {
          const task = automatic && [...related.values()].find(item => item.url === href)
          const label = automatic ? task?.key || sourceLinkLabel(href) : opened.children
          parent.push(<BoardLink key={opened.index} href={href} title={opened.token.attrGet('title') || undefined}>{label}</BoardLink>)
        } else {
          parent.push(...opened.children)
        }
      } else if (tag === 'table') {
        parent.push(
          <div key={opened.index} className="markdown-table" role="region" aria-label="Scrollable table" tabIndex={0}>
            <table>{opened.children}</table>
          </div>,
        )
      } else if (!hidden && elements.has(tag)) {
        const start = tag === 'ol' ? opened.token.attrGet('start') : null
        parent.push(React.createElement(tag, { key: opened.index, ...(start ? { start } : {}) }, opened.children))
      } else {
        parent.push(...opened.children)
      }
    } else {
      switch (token.type) {
        case 'inline':
          current.children.push(<React.Fragment key={index}>{tokensToElements(token.children, links, related)}</React.Fragment>)
          break
        case 'text': case 'text_special':
          if (links && !current.inLink && related.size) current.children.push(...linkedTickets(token.content, related, index))
          else current.children.push(token.content)
          break
        case 'softbreak': current.children.push('\n'); break
        case 'hardbreak': current.children.push(<br key={index} />); break
        case 'hr': current.children.push(<hr key={index} />); break
        case 'code_inline': current.children.push(<code key={index}>{token.content}</code>); break
        case 'fence': case 'code_block':
          current.children.push(token.type === 'fence' && token.info.trim() === 'mermaid'
            ? <MermaidDiagram key={`${index}:${token.content}`} text={token.content} />
            : <pre key={index}><code>{token.content}</code></pre>)
          break
        case 'image': {
          const href = links && !current.inLink && markdownHref(token.attrGet('src'))
          const label = token.content || (href && sourceLinkLabel(href))
          if (href) current.children.push(<BoardLink key={index} href={href}>{label}</BoardLink>)
          else current.children.push(token.content)
          break
        }
      }
    }
  }
  return frames[0].children
}

export function MarkdownInline({ text, links = true, relatedTasks = [] }) {
  return tokensToElements(markdown.parseInline(text ?? '', {}), links,
    new Map(relatedTasks.map(task => [task.key, task])))
}

export function Markdown({ text, className = 'prose', relatedTasks = [], links = true }) {
  const related = new Map(relatedTasks.map(task => [task.key, task]))
  return <div className={className + ' markdown'}>{tokensToElements(markdown.parse(text ?? '', {}), links, related)}</div>
}
