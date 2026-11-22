import { request } from './data.js'

const retryDelay = 5_000
const requestTimeout = 15_000

export function isUncertainDecision(error) {
  return error.status == null || error.status >= 500 || [408, 409, 429].includes(error.status)
}

export async function decisionRequest(path, controller, options = {}) {
  const timeout = setTimeout(() => controller.abort(), requestTimeout)
  try {
    return await request(path, { ...options, signal: controller.signal })
  } finally {
    clearTimeout(timeout)
  }
}

export function watchDecision(path, onResult) {
  let stopped = false
  let retry
  let active

  async function check() {
    active = new AbortController()
    let result
    try {
      const receipt = await decisionRequest(path, active, { cache: 'no-store' })
      if (receipt?.status === 'open' || receipt?.status === 'closed') result = receipt
      if (receipt?.status === 'answered' && Number.isSafeInteger(receipt.answer?.event_id)
        && typeof receipt.answer.note === 'string') result = receipt
    } catch (error) {
      if ([401, 403, 404].includes(error.status)) result = { status: 'unavailable' }
    }
    if (stopped) return
    if (!result || onResult(result) === false) retry = setTimeout(check, retryDelay)
  }

  check()
  return () => {
    stopped = true
    clearTimeout(retry)
    active?.abort()
  }
}
