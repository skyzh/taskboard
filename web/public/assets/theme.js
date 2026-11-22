(() => {
  let preference
  try {
    preference = localStorage.getItem('tasks-board.theme')
  } catch {
    // System appearance remains available when storage is blocked.
  }
  const root = document.documentElement
  if (preference === 'day' || preference === 'night') root.dataset.theme = preference
  const dark = preference === 'night' ||
    (preference !== 'day' && matchMedia('(prefers-color-scheme: dark)').matches)
  root.dataset.colorScheme = dark ? 'dark' : 'light'
  const scheme = document.querySelector('meta[name="color-scheme"]')
  if (scheme) scheme.content = root.dataset.colorScheme
})()
