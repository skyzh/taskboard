const scope = self.registration.scope
const inbox = new URL('#notifications-needs-me', scope).href

self.addEventListener('push', event => {
  event.waitUntil((async () => {
    try {
      const enrollment = event.data?.json()?.enrollment
      if (!enrollment) return
      const response = await fetch(new URL('api/v1/notifications/browser-push', scope), {
        credentials: 'same-origin',
        cache: 'no-store',
      })
      if (!response.ok || (await response.json()).enrollment !== enrollment) return
      await self.registration.showNotification('Taskboard', { body: 'You have a new request.' })
    } catch {
      // An unverifiable enrollment must not display an alert.
    }
  })())
})

self.addEventListener('notificationclick', event => {
  event.notification.close()
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    const existing = windows.find(client => client.url === inbox)
    return existing ? existing.focus() : self.clients.openWindow(inbox)
  })())
})
