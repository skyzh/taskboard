const walkthrough = document.querySelector('.walkthrough')
const toggle = walkthrough.querySelector('.motion-toggle')

function setMotion(playing) {
  walkthrough.dataset.motion = playing ? 'playing' : 'paused'
  toggle.textContent = playing ? 'Pause animation' : 'Play animation'
}

toggle.addEventListener('click', () => setMotion(walkthrough.dataset.motion !== 'playing'))
toggle.hidden = false
setMotion(true)
