import { readFileSync } from 'node:fs'

const inventory = JSON.parse(readFileSync(process.argv[2], 'utf8'))
const runtime = inventory.runtimes.find(item => item.isAvailable && item.version === '26.5' && item.name.includes('iOS'))
if (!runtime) throw new Error('iOS 26.5 is unavailable in the runner inventory')
const phone = inventory.devices[runtime.identifier]?.find(item => item.isAvailable && item.name === 'iPhone 17')
if (!phone) throw new Error('iPhone 17 is unavailable in the iOS 26.5 runtime')
console.log(phone.udid, runtime.version, runtime.buildversion)
