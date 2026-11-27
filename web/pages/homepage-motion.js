const reviewLoop = document.querySelector('.review-loop')
const observer = new IntersectionObserver(entries => {
  if (!entries.some(entry => entry.isIntersecting)) return
  reviewLoop.classList.add('is-visible')
  observer.disconnect()
}, { threshold: 0.5 })

observer.observe(reviewLoop)
