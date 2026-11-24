const taskFragment = /^#task-([A-Za-z][A-Za-z0-9]{0,7}-)?([1-9]\d*)$/
const activityFragment = /^#event-([1-9]\d*)-log$/
const githubIssueOrPull = /^\/([\w.-]+\/[\w.-]+)\/(pull|issues)\/([1-9]\d*)(?:\/|$)/
const githubCommit = /^\/([\w.-]+\/[\w.-]+)\/commit\/([a-f\d]{7,40})(?:\/|$)/i
const stampPull = /^\/(?:s\/[\w.-]+\/)?github\/([\w.-]+\/[\w.-]+)\/pull\/([1-9]\d*)(?:\/|$)/

export function markdownHref(href) {
  if (typeof href !== 'string') return null
  if (taskFragment.test(href) || activityFragment.test(href)) return href
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
  const target = githubIssueOrPull.exec(url.pathname)
  if (!target) return label || href
  const [, repository, kind, number] = target
  if (label && ![href, url.href, 'GitHub', 'GitHub · ' + repository + '#' + number].includes(label)) return label
  return (kind === 'pull' ? 'Pull request ' : 'Issue ') + repository + ' #' + number
}

function sourceRecord(href, board) {
  const url = URL.canParse(href) ? new URL(href) : null
  const fragment = href.startsWith('#') ? href
    : url && url.origin === board?.origin && url.pathname === board.pathname ? url.hash : ''
  const task = taskFragment.exec(fragment)
  if (task) return { kind: 'Ticket', reference: `${(task[1] || 'T-').toUpperCase()}${task[2]}` }
  const activity = activityFragment.exec(fragment)
  if (activity) return { kind: 'Activity', reference: `Activity #${activity[1]}` }

  if (url?.protocol !== 'https:' || url.port || url.username || url.password) return null
  if (url.hostname === 'github.com') {
    const issueOrPull = githubIssueOrPull.exec(url.pathname)
    if (issueOrPull) return { kind: issueOrPull[2] === 'pull' ? 'Pull request' : 'Issue', reference: `${issueOrPull[1]}#${issueOrPull[3]}` }
    const commit = githubCommit.exec(url.pathname)
    if (commit) return { kind: 'Commit', reference: `${commit[1]}@${commit[2].slice(0, 7)}` }
  }
  if (url.hostname === 'stamp.build') {
    const pull = stampPull.exec(url.pathname)
    if (pull) return { kind: 'Stamp review', reference: `${pull[1]}#${pull[2]}` }
  }
  return null
}

export function evidenceLinkLabel(href, label, { generated, board } = {}) {
  const record = sourceRecord(href, board)
  if (!record) return label || href
  const { kind, reference } = record
  if (!label || (generated && (label === href || label === 'GitHub' || label === `GitHub · ${reference}`))) {
    return kind === 'Activity' ? reference : `${kind} · ${reference}`
  }
  const escapedReference = reference.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const commitTail = kind === 'Commit' ? '[a-f\\d]{0,33}' : ''
  const alreadyNamed = new RegExp(`(^|[^\\w./-])${escapedReference}${commitTail}(?![\\w/-])`, 'i')
    .test(label.replace(/\s+#/g, '#').replace(/Activity#/gi, 'Activity #'))
  return alreadyNamed ? label : `${label} · ${reference}`
}
