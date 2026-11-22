import { cpSync } from 'node:fs'

cpSync(new URL('../public/assets/licenses', import.meta.url),
  new URL('../dist-demo/assets/licenses', import.meta.url), { recursive: true })
