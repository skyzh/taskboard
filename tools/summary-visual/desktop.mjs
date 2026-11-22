import assert from 'node:assert/strict'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const [module, url, evidence] = process.argv.slice(2)
const { chromium } = await import(pathToFileURL(module).href)
const browser = await chromium.launch()
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 850 } })
  const metrics = page.waitForResponse(response =>
    new URL(response.url()).pathname === '/tasks/__measure/desktop' && response.ok(), { timeout: 30000 })
  const rendered = await page.goto(`${url}?layout=desktop#reports?project=T`)
  assert.ok(rendered.ok())
  await metrics
  await page.screenshot({ path: join(evidence, 'desktop.png') })
  console.log(`Desktop Chromium ${browser.version()}`)
} finally {
  await browser.close()
}
