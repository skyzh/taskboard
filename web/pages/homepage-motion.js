const flow = document.querySelector('.setup-flow')
const toggle = flow.querySelector('.motion-toggle')

function setMotion(playing) {
  flow.dataset.motion = playing ? 'playing' : 'paused'
  toggle.textContent = playing ? 'Pause animation' : 'Play animation'
}

toggle.addEventListener('click', () => setMotion(flow.dataset.motion !== 'playing'))
toggle.hidden = false
setMotion(true)
