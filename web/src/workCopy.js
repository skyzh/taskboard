export function withoutRepeatedTitle(note, title) {
  const text = note.trim()
  const heading = title.trim()
  if (!heading || !text.toLowerCase().startsWith(heading.toLowerCase())) return text
  if (text.length === heading.length) return ''
  const rest = text.slice(heading.length)
  if (/^[.!?:;](?:\s|$)|^\n|^\s+[—–-]\s/.test(rest)) {
    return rest.replace(/^[\s.!?:;—–-]+/, '')
  }
  return text
}

export function briefSourceText(value) {
  const text = value?.trim()
  if (!text || /[\r\n]|(?:\.\.\.|…)$/u.test(text) || Array.from(text).length > 140) return ''
  return text
}

function reportNote(note, title) {
  if (!title || !note.startsWith(title)) return note
  const rest = note.slice(title.length)
  if (!rest) return ''
  const separator = /^(?:[.!?:;](?=\s|$)|\r?\n|[ \t]+[—–-](?=[ \t]))/.exec(rest)
  return separator ? rest.slice(separator[0].length).trimStart() : note
}

export function reportTaskCue(task, parkedRequest = false) {
  if (!parkedRequest && !['ready', 'in_progress', 'waiting', 'human_review'].includes(task.state)) return null
  const text = reportNote(task.note ?? '', task.title)
  const label = task.state === 'human_review' ? undefined : task.summary_label
  const authoredDetails = task.details ?? task.context?.activity_details
  if (typeof authoredDetails === 'string') {
    return { text, label, details: authoredDetails.trim() === text.trim() ? '' : authoredDetails }
  }

  const next = /(^|[.!?][ \t]+|\r?\n)[ \t]*next(?: step)?:[ \t]*/i.exec(text)
  if (next) {
    const block = text.slice(next.index + next[0].length).trim()
    if (block) {
      return { text: block, label: 'Next step', details: text.slice(0, next.index + next[1].length).trimEnd() }
    }
  }

  const legacyReceipt = /^Review exact [1-9]\d*-file [a-fA-F0-9]{64} in \/(?:[^/\s]+\/)+[^/\s]+?(?:\.(?=\s|$)|(?=\r?\n|$))/
  if (task.state === 'waiting' && task.waiting_on && legacyReceipt.test(text)) {
    return { text: '', label, details: task.note }
  }
  return { text, label }
}

export function activityResult(event) {
  let note = event.note
  if (event.context?.worker_report_id || event.context?.worker_disposition_id) {
    const [, , , ...report] = note.split(' · ')
    if (report.length) note = report.join(' · ')
  }
  const result = withoutRepeatedTitle(note, event.task_title)
  return result.replace(
    /^(?:as requested, )?(?:i|we) (?:have )?(?:now )?(?:successfully )?completed the (?:requested )?(?:work|task)(?: and verified the final outcomes as appropriate)?[.!]\s+/i,
    '',
  )
}

export function waitLabel(record) {
  const kind = record.wait_kind ?? record.context?.wait_kind
  if (record.blockers || kind === 'ticket' || kind === 'automation') return 'Blocked by'
  if (kind === 'person') return 'Waiting on'
  if (kind === 'attention' || record.state === 'human_review') return 'Assigned to'
  const profile = record.waiting_profile
  if (!kind && profile?.id === record.waiting_on && ['agent', 'human'].includes(profile?.kind)) return 'Waiting on'
  return 'Wait note'
}

function waitHeadline(event) {
  const owner = event.waiting_profile?.name || event.waiting_on
  return owner ? waitLabel(event) + ' ' + owner : 'Waiting'
}

export function hasExpandedDecisionText(event) {
  return event.action === 'decide' && (event.note.length > 180 || /[\r\n]/.test(event.note))
}

export function activityHeadline(event) {
  const context = event.context
  if (context?.ownership_action === 'reassign' || (
    event.action === 'transfer' && !context?.ownership_action && context?.previous_dri !== undefined
  )) return 'Ticket reassigned'
  const audit = context?.audit
  if (audit?.action === 'edit-task-details') {
    const titleChanged = Object.hasOwn(audit.current || {}, 'title')
    const briefChanged = Object.hasOwn(audit.current || {}, 'brief')
    if (titleChanged && briefChanged) return 'Title and brief updated'
    if (titleChanged) return 'Title updated'
    if (briefChanged) return 'Brief updated'
  }
  if (hasExpandedDecisionText(event)) return event.label || 'Decision recorded'
  if (event.headline) {
    if (event.action !== 'wait') return event.headline
    const prior = 'Waiting on ' + (event.waiting_profile?.name || event.waiting_on)
    if (event.headline === prior || event.headline.startsWith(prior + ' · ')) {
      return waitHeadline(event) + event.headline.slice(prior.length)
    }
    return event.headline
  }

  const sentences = activityResult(event).split(/(?<=[.!?])\s+|\r?\n/).map(line => line.trim()).filter(Boolean)
  const hold = sentences.find(line => /^(?:hold\b|do not (?:activate|deploy|launch|merge|publish|release|roll out|ship)\b)/i.test(line))
  const first = hold || sentences[0] || ''
  const technical = /(?:^|\s)(?:file:\/\/|\/|[a-z]:\\)|\b[0-9a-f]{32,}\b|^W-\d+\s+·/i.test(first)
  const summary = !technical && first.length <= 180 ? first : ''

  if (hold) return summary || 'Hold recorded'
  if (event.action === 'wait') {
    const waiting = waitHeadline(event)
    if (/^waiting\b/i.test(summary)) return summary
    return summary && waiting.length + summary.length + 3 <= 180 ? waiting + ' · ' + summary : waiting
  }
  return summary || event.label || 'Activity'
}

export function activityExtraText(event, headline) {
  const detail = (event.details ?? event.note).trim()
  const summary = headline.trim()
  if (!summary || !detail.startsWith(summary)) return detail
  const remainder = detail.slice(summary.length)
  return !remainder || /^\s/.test(remainder) ? remainder.trimStart() : detail
}

export function ticketActivityCopy(event, headline) {
  const choice = event.context?.choice
  const withoutPunctuation = text => text.trim().replace(/[.!?]+$/, '')
  const comparable = text => withoutPunctuation(text).toLowerCase()
  const result = choice && (comparable(choice) === comparable(headline) || headline === event.label) ? '' : headline
  if (hasExpandedDecisionText(event)) return { choice, result, details: event.details }

  const waitPrefix = waitHeadline(event) + ' · '
  const shownResult = event.action === 'wait' && result.startsWith(waitPrefix) ? result.slice(waitPrefix.length) : result
  const shown = [shownResult, choice].filter(Boolean)

  if (event.details != null) {
    const details = shown.reduce((text, item) => withoutRepeatedTitle(text, withoutPunctuation(item)), event.details)
    return { choice, result, details }
  }

  const duplicates = new Set(shown.map(comparable))
  const details = activityResult(event).split(/(?<=[.!?])(?=\s)|(?<=\n)/)
    .filter(part => !duplicates.has(comparable(part))).join('').trim()
  return { choice, result, details }
}
