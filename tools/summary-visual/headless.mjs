import assert from 'node:assert/strict'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const [module, url, evidence, ...layouts] = process.argv.slice(2)
const { chromium, devices, webkit } = await import(pathToFileURL(module).href)
const phone = devices['iPhone 16 Pro']
const viewports = {
  desktop: { viewport: { width: 1280, height: 850 } },
  compact: phone,
  wide: { ...phone, viewport: { ...phone.viewport, width: 768 } },
}

for (const engine of [chromium, webkit]) {
  const engineLayouts = layouts.filter(layout => (layout === 'desktop') === (engine === chromium))
  if (!engineLayouts.length) continue
  const browser = await engine.launch()
  try {
    for (const layout of engineLayouts) {
      const page = await browser.newPage(viewports[layout])
      try {
        const [, rendered] = await Promise.all([
          page.waitForResponse(response =>
            new URL(response.url()).pathname === `/tasks/__measure/${layout}` && response.ok(), { timeout: 30000 }),
          page.goto(`${url}?layout=${layout}#reports?project=T`),
        ])
        assert.ok(rendered.ok())
        await page.screenshot({ path: join(evidence, `${layout}.png`) })
      } finally {
        await page.close()
      }
    }
    console.log(`Headless ${engine.name()} ${browser.version()}: ${engineLayouts.join(', ')}`)
  } finally {
    await browser.close()
  }
}
