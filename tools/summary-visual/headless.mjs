import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
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

async function checkTableKeyboard(page) {
  const fixtures = JSON.parse(await readFile(new URL('synthetic-api.json', import.meta.url), 'utf8'))
  const reports = fixtures['/tasks/api/v1/reports?project=T&limit=20']
  const task = reports.items.flatMap(report => report.tasks).find(item => item.key === 'T-1')
  task.note = '| First column | Environment | Verification | Last column |\n' +
    '| --- | --- | --- | --- |\n' +
    '| Start here | Fictional | LONG_UNBROKEN_REFERENCE_FOR_THE_COMPACT_TABLE_TEST | End here |'
  await page.route(url => url.pathname === '/tasks/api/v1/reports', route => route.fulfill({ json: reports }))
  await page.route(url => /^\/tasks\/api\/v1\/tasks\/(?:T-)?1$/.test(url.pathname), route => route.fulfill({
    json: { task, brief: '', checklist: [], sources: [], related_tasks: [], workers: [] },
  }))
  await page.goto(`${url}#reports?project=T`)

  const summaryRow = page.locator('.report-task-summary').filter({ has: page.getByRole('link', { name: /^T.1 / }) })
  const excerpt = summaryRow.locator('.summary-next-step')
  await excerpt.waitFor()
  assert.equal(await excerpt.locator('a, button, [tabindex]').count(), 0)
  assert.match(await excerpt.textContent(), /LONG_UNBROKEN_REFERENCE_FOR_THE_COMPACT_TABLE_TEST/)
  await summaryRow.getByRole('link', { name: /^T.1 / }).press('Enter')

  const region = page.getByRole('region', { name: 'Scrollable table' })
  await region.focus()
  const max = await region.evaluate(node => node.scrollWidth - node.clientWidth)
  assert.ok(max > 0)
  const lastColumn = region.getByRole('cell', { name: 'End here' })
  const showsLastColumn = () => lastColumn.evaluate(node =>
    node.getBoundingClientRect().right <= node.closest('.markdown-table').getBoundingClientRect().right + 1)
  assert.equal(await showsLastColumn(), false)
  for (let press = 0; press < Math.ceil(max / 40); press++) await region.press('ArrowRight')
  assert.equal(await showsLastColumn(), true)
  assert.ok(await region.evaluate(node => document.activeElement === node))
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
        if (layout === 'compact') await checkTableKeyboard(page)
      } finally {
        await page.close()
      }
    }
    console.log(`Headless ${engine.name()} ${browser.version()}: ${engineLayouts.join(', ')}`)
  } finally {
    await browser.close()
  }
}
