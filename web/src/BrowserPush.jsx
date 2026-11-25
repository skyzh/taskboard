import React, { useEffect, useState } from 'react'
import { request } from './data.js'

const path = '/notifications/browser-push'

function applicationKey(value) {
  return Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/')), character =>
    character.charCodeAt(0))
}

function Enrollment() {
  const [settings, setSettings] = useState(null)
  const [registration, setRegistration] = useState(null)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    async function load() {
      try {
        const data = await request(path, { signal: controller.signal })
        if (!data?.available) return
        await navigator.serviceWorker.register('./ask-sw.js')
        const worker = await navigator.serviceWorker.ready
        if (!controller.signal.aborted) {
          setRegistration(worker)
          setSettings(data)
        }
      } catch (failure) {
        if (!controller.signal.aborted && failure.status !== 404) {
          setError('Browser notifications could not be set up in this browser.')
        }
      }
    }
    load()
    return () => controller.abort()
  }, [])

  async function enable() {
    setSaving(true)
    setError('')
    try {
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: applicationKey(settings.public_key),
      })
      setSettings(await request(path, { method: 'PUT', body: subscription.toJSON() }))
    } catch (failure) {
      setError(Notification.permission === 'denied'
        ? 'Notifications are blocked. Allow them in this browser’s settings and try again.'
        : failure.status === 401
          ? 'Sign in again to enable notifications on this browser.'
          : 'Could not enable browser notifications. Try again.')
    } finally {
      setSaving(false)
    }
  }

  async function disable() {
    setSaving(true)
    setError('')
    try {
      setSettings(await request(path, { method: 'DELETE' }))
      const subscription = await registration.pushManager.getSubscription()
      await subscription?.unsubscribe()
    } catch {
      setError('Could not finish turning off browser notifications. Check the current setting and try again.')
    } finally {
      setSaving(false)
    }
  }

  if (!settings && !error) return null
  return (
    <details className="browser-push-settings">
      <summary>Browser notifications</summary>
      <p>Get a generic alert for new Decisions and attention requests, even when the Taskboard tab is closed.</p>
      {settings && <button type="button" disabled={saving} onClick={settings.enrollment ? disable : enable}>
        {settings.enrollment ? 'Turn off on this browser' : 'Enable on this browser'}
      </button>}
      {error && <p role="alert">{error}</p>}
    </details>
  )
}

export default function BrowserPush() {
  if (typeof window === 'undefined') return null
  if ('serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window) return <Enrollment />
  return (
    <details className="browser-push-settings">
      <summary>Browser notifications</summary>
      <p>Use a browser that supports Web Push. On iPhone or iPad, add Taskboard to the Home Screen and open it there.</p>
    </details>
  )
}
