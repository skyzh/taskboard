const reviewLoop = document.querySelector('.review-loop')
const motionToggle = reviewLoop.querySelector('.loop-motion-toggle')
motionToggle.hidden = false
motionToggle.addEventListener('click', () => {
  const paused = reviewLoop.classList.toggle('is-paused')
  motionToggle.textContent = paused ? 'Resume animation' : 'Pause animation'
})

const observer = new IntersectionObserver(entries => {
  if (!entries.some(entry => entry.isIntersecting)) return
  reviewLoop.classList.add('is-visible')
  observer.disconnect()
}, { threshold: 0.5 })

observer.observe(reviewLoop)
