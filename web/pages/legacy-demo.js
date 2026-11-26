// The previous Pages root used fragments to open specific demo views.
if (window.location.hash.length > 1) {
  const demo = new URL('./demo.html', window.location.href)
  demo.search = window.location.search
  demo.hash = window.location.hash
  window.location.replace(demo)
}
