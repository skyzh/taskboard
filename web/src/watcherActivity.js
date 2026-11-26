const linkedSourceActors = new Set(['GitHub', 'Jira', 'Local file'])
const checkOccurrenceFields = new Set(['check_run_id', 'github_activity_id', 'provider_at'])

function watcherKey(event) {
  const { action, actor, context = {}, links = [], headline, note } = event
  const title = headline ?? note
  if (!title || context.checkpoint || context.audit || context.work_kind || context.choice) return null

  const entry = [event.task_id, actor, action, event.state, event.waiting_on, title, event.details]
  const check = action === 'log' && context.extension === 'github-events' && context.github_event === 'ci-job'
  if (check && context.github_source && context.head_sha && context.job && context.conclusion) {
    // Each rerun has its own time, receipt and job link. Compare its result and other evidence.
    const result = Object.entries(context).filter(([field]) => !checkOccurrenceFields.has(field))
    const evidence = links.filter(link => link.label !== 'GitHub check')
    return JSON.stringify([...entry, result, evidence])
  }

  const legacySource = action === 'source-event' && linkedSourceActors.has(actor) && !context.event_kind
  const legacyCheck = action === 'log' && actor === 'GitHub extension' &&
    (check || !context.github_event && !context.extension) && links.some(link => link.label === 'GitHub check')
  if ((!legacySource && !legacyCheck) || !links.some(link => link.url && !link.unavailable)) return null
  return JSON.stringify([...entry, context, links])
}

export function groupWatcherActivity(events) {
  const groups = []
  let previousKey = null
  for (const event of events) {
    const key = watcherKey(event)
    if (key !== null && key === previousKey) groups.at(-1).push(event)
    else groups.push([event])
    previousKey = key
  }
  return groups
}
