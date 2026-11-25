const projectPrefix = /^[A-Z][A-Z0-9]{0,7}$/

export function projectFilters(project) {
  return project ? { project } : { scope: 'all' }
}

export function validProject(value) {
  return projectPrefix.test(value || '')
}

// The application uses both named fragments and older query-only links.
export function projectHref(href, scope, { replace = false } = {}) {
  if (!scope) return href
  const [path, query] = href.slice(1).split('?', 2)
  const named = !path.includes('=') && !path.includes('&')
  const params = new URLSearchParams(named ? query : path)
  if (!replace && (params.has('project') || params.get('scope') === 'all' || path === 'reports-all')) return href

  params.delete('project')
  if (params.get('scope') === 'all') params.delete('scope')
  let target = path
  if (path === 'reports' || path === 'reports-all') {
    target = scope === 'all' ? 'reports-all' : 'reports'
    if (params.get('view') === 'goal-activity') {
      target = 'reports'
      if (scope === 'all') params.set('scope', 'all')
    }
  } else if (scope === 'all' && !params.has('scope')) params.set('scope', 'all')
  if (scope !== 'all') params.set('project', scope)
  return named ? '#' + target + (params.size ? '?' + params : '') : '#' + params
}
