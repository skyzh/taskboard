import { useEffect, useRef, useState } from 'react'

let activeMutations = 0
let previewRequest

export function configurePreviewData(readRequest) {
  previewRequest = readRequest
}

export function queryPath(path, filters = {}) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== '') params.set(key, value)
  }
  return params.size ? `${path}?${params}` : path
}

export async function request(path, { signal, cache, body, method = body ? 'POST' : 'GET' } = {}) {
  if (previewRequest) return previewRequest(path, method)
  const multipart = body instanceof FormData
  const mutation = method.toUpperCase() !== 'GET'
  if (mutation) activeMutations += 1
  try {
    const response = await fetch('./api/v1' + path, {
      signal,
      cache,
      method,
      credentials: 'same-origin',
      ...(body && (multipart ? { body } : {
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      })),
    })
    const data = response.status === 204 ? null : await response.json().catch(() => null)
    if (!response.ok) {
      const message = data?.message || data?.error
      const error = new Error(typeof message === 'string' ? message : `Request failed (${response.status}).`)
      error.status = response.status
      if (typeof data?.code === 'string') error.code = data.code
      throw error
    }
    if (response.status !== 204 && data === null) throw new Error('The server returned an invalid response.')
    return data
  } finally {
    if (mutation) activeMutations -= 1
  }
}

export async function signOut() {
  if (previewRequest) return previewRequest('/auth/logout', 'POST')
  activeMutations += 1
  try {
    const response = await fetch('./auth/logout', { method: 'POST', credentials: 'same-origin' })
    if (response.status !== 204) throw new Error('Sign out failed.')
  } finally {
    activeMutations -= 1
  }
}

export function watchFrontendBuild(onDeferred) {
  const loaded = document.querySelector('meta[name="tasks-frontend-build"]')?.content
  if (!loaded) return
  let active
  let reloading = false

  async function poll() {
    if (document.visibilityState !== 'visible' || active || reloading) return
    const controller = new AbortController()
    active = controller
    try {
      const { build } = await request('/frontend-build', { signal: controller.signal, cache: 'no-store' })
      if (controller.signal.aborted || document.visibilityState !== 'visible') return
      if (typeof build !== 'string' || !/^[a-f0-9]{64}$/.test(build)) return
      if (build === loaded) {
        onDeferred(false)
      } else if (activeMutations || document.querySelector('[data-reload-blocked="true"]')) {
        onDeferred(true)
      } else {
        reloading = true
        window.location.reload()
      }
    } catch {
      // A disconnected board can check again without interrupting the current page.
    } finally {
      if (active === controller) active = null
    }
  }

  function visibilityChanged() {
    active?.abort()
    active = null
    poll()
  }

  document.addEventListener('visibilitychange', visibilityChanged)
  const timer = window.setInterval(poll, 5_000)
  poll()
  return () => {
    window.clearInterval(timer)
    document.removeEventListener('visibilitychange', visibilityChanged)
    active?.abort()
  }
}

export function watchBoardRevision(onChange, onStatus, { websocket = false } = {}) {
  let previous
  let unavailable = false
  let timer
  let active
  let stream

  function observed(revision, source) {
    if (typeof revision !== 'string') throw new Error('The server did not return a revision.')
    if (previous !== revision || unavailable) onChange()
    previous = revision
    unavailable = false
    onStatus?.({ stale: false, checking: false, checkedAt: new Date().toISOString(), source })
  }

  async function poll() {
    if (document.visibilityState !== 'visible' || active || stream?.readyState === 1) return
    const controller = new AbortController()
    active = controller
    onStatus?.({ checking: true })
    try {
      const { revision } = await request('/revision', { signal: controller.signal })
      if (controller.signal.aborted) return
      observed(revision, 'poll')
    } catch {
      if (!controller.signal.aborted) {
        unavailable = true
        onStatus?.({ stale: true, checking: false })
      }
    } finally {
      if (active === controller) active = null
    }
  }

  function check() {
    if (websocket && !stream && typeof window.WebSocket === 'function') {
      const url = new URL('./api/v1/revisions', window.location.href)
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
      let next
      try {
        next = new window.WebSocket(url)
      } catch {
        return poll()
      }
      stream = next
      next.onopen = () => {
        if (stream === next) {
          active?.abort()
          active = null
        }
      }
      next.onmessage = event => {
        if (stream !== next) return
        try {
          observed(JSON.parse(event.data).revision, 'socket')
        } catch {
          next.close()
        }
      }
      next.onclose = () => {
        if (stream === next) stream = null
      }
    }
    return poll()
  }

  function stop() {
    window.clearInterval(timer)
    active?.abort()
    active = null
    const previousStream = stream
    stream = null
    previousStream?.close()
  }

  function visibilityChanged() {
    stop()
    if (document.visibilityState === 'visible') {
      check()
      timer = window.setInterval(check, 5_000)
    }
  }

  document.addEventListener('visibilitychange', visibilityChanged)
  visibilityChanged()
  return () => {
    document.removeEventListener('visibilitychange', visibilityChanged)
    stop()
  }
}

export function useResource(path, revision = 0) {
  const [attempt, setAttempt] = useState(0)
  const [result, setResult] = useState(null)

  useEffect(() => {
    if (!path) return
    const controller = new AbortController()
    const complete = outcome => {
      if (!controller.signal.aborted) setResult({ path, revision, attempt, ...outcome })
    }
    request(path, { signal: controller.signal }).then(
      data => complete({ data }),
      error => complete({ error }),
    )
    return () => controller.abort()
  }, [path, revision, attempt])

  const current = result && result.path === path && result.revision === revision && result.attempt === attempt
  return {
    data: result && result.path === path ? result.data : undefined,
    error: current ? result.error : undefined,
    pending: Boolean(path && !current),
    retry: () => setAttempt(value => value + 1),
  }
}

const identity = value => value

export function useLoadedPages(path, filters, revision, {
  limit = 20, moreLimit = limit, cursorParam = 'before', readPage = identity, firstPage, enabled = true,
  rememberedNavigation,
} = {}) {
  const base = path ? queryPath(path, { ...filters, limit }) : null
  const continuation = path ? queryPath(path, { ...filters, limit: moreLimit }) : null
  const [navigation, setNavigation] = useState(() => rememberedNavigation?.current[base] || { base, count: 1 })
  const [attempt, setAttempt] = useState(0)
  const [result, setResult] = useState(null)
  const cached = useRef(null)
  const count = navigation.base === base ? navigation.count : rememberedNavigation?.current[base]?.count ?? 1
  const key = JSON.stringify([base, count, revision, attempt])

  useEffect(() => {
    if (!base || !enabled) return
    const controller = new AbortController()
    const previous = cached.current
    const reusable = previous?.base === base && previous.revision === revision && previous.firstPage === firstPage
    let loaded = reusable ? previous.loaded : 0
    let data = reusable ? previous.data : undefined

    async function load() {
      try {
        while (loaded < count && (loaded === 0 || data.next_cursor != null)) {
          const cursor = data?.next_cursor
          const url = cursor == null ? base : continuation + '&' + new URLSearchParams({ [cursorParam]: cursor })
          const page = loaded === 0 && firstPage ? firstPage : readPage(await request(url, { signal: controller.signal }))
          const items = [...(data?.items || []), ...page.items]
          // Changes in priority, execution, or active work can move rows past a cursor.
          const rowKey = path === '/reports' ? 'owner' : 'id'
          data = { ...page, items: cursor != null && ['/tasks', '/reports/workers', '/reports'].includes(path)
            ? [...new Map(items.map(item => [item[rowKey], item])).values()]
            : items }
          loaded += 1
        }
        if (controller.signal.aborted) return
        const snapshot = { base, revision, firstPage, loaded, data }
        cached.current = snapshot
        setResult({ key, firstPage, snapshot })
      } catch (error) {
        if (!controller.signal.aborted) setResult({ key, firstPage, snapshot: previous, error })
      }
    }

    load()
    return () => controller.abort()
  }, [base, continuation, count, cursorParam, enabled, firstPage, key, path, readPage, revision])

  const pending = Boolean(base && (!enabled || result?.key !== key || result.firstPage !== firstPage))
  const snapshot = result?.snapshot?.base === base ? result.snapshot : null
  const error = pending ? null : result?.error
  const moreError = error && snapshot?.data.next_cursor != null && count > snapshot.loaded ? error : null
  return {
    data: snapshot?.data ?? firstPage,
    pending,
    loadingMore: pending && Boolean(snapshot && count > snapshot.loaded),
    error: moreError ? null : error,
    moreError,
    retry: () => setAttempt(number => number + 1),
    loadMore: !pending && !error && snapshot?.data.next_cursor != null
      ? () => {
          const next = { base, count: count + 1 }
          if (rememberedNavigation) rememberedNavigation.current[base] = next
          setNavigation(next)
        }
      : null,
  }
}
