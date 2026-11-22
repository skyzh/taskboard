export function markdownHref(href) {
  if (typeof href !== 'string') return null
  if (/^#(?:task-(?:[A-Za-z][A-Za-z0-9]{0,7}-)?[1-9]\d*|event-[1-9]\d*-log)$/.test(href)) return href
  if (!URL.canParse(href)) return null
  const url = new URL(href)
  if ((url.protocol !== 'https:' && url.protocol !== 'http:') || url.username || url.password) return null
  return href
}

export function sourceLinkLabel(href, label) {
  if (!URL.canParse(href)) return label || href
  const url = new URL(href)
  if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port || url.username || url.password) {
    return label || href
  }
  const target = /^\/([\w.-]+\/[\w.-]+)\/(pull|issues)\/([1-9]\d*)(?:\/|$)/.exec(url.pathname)
  if (!target) return label || href
  const [, repository, kind, number] = target
  if (label && ![href, url.href, 'GitHub', 'GitHub · ' + repository + '#' + number].includes(label)) return label
  return (kind === 'pull' ? 'Pull request ' : 'Issue ') + repository + ' #' + number
}
