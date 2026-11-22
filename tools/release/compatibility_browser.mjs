import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'

const [module, origin, expectedHtml, expectedTitle, manifest] = process.argv.slice(2)
const manifestRows = (await readFile(manifest, 'utf8')).trim().split('\n')
const expectedAssets = new Map(manifestRows.map(row => [row.slice(66), row.slice(0, 64)]))
assert.equal(expectedHtml, expectedAssets.get('web/dist/index.html'))
const { chromium } = await import(pathToFileURL(module).href)
const browser = await chromium.launch({ headless: true })
try {
  const context = await browser.newContext({
    ignoreHTTPSErrors: true,
    extraHTTPHeaders: { 'X-Portal-Upstream-Token': 'synthetic-portal-fixture' },
  })
  const page = await context.newPage()
  const errors = []
  const fetchedAssets = []
  page.on('response', response => {
    const url = new URL(response.url())
    if (url.origin !== origin || !url.pathname.startsWith('/tasks/assets/')) return
    const member = 'web/dist/' + url.pathname.slice('/tasks/'.length)
    fetchedAssets.push(response.body().then(bytes => {
      const digest = createHash('sha256').update(bytes).digest('hex')
      assert.equal(digest, expectedAssets.get(member))
      return [member, digest]
    }))
  })
  page.on('pageerror', error => errors.push(error.message))
  const task = page.waitForResponse(
    response => new URL(response.url()).pathname === '/tasks/api/v1/tasks/2' && response.ok(),
    { timeout: 10000 },
  )
  const initial = await page.goto(origin + '/tasks/#task-2', { timeout: 10000 })
  const digest = createHash('sha256').update(await initial.body()).digest('hex')
  assert.equal(digest, expectedHtml)
  assert.equal((await (await task).json()).task.title, expectedTitle)
  await page.getByRole('heading', { name: expectedTitle, exact: true }).waitFor({ timeout: 10000 })
  assert.deepEqual(errors, [])
  const assets = Object.fromEntries(await Promise.all(fetchedAssets))
  assert.ok(Object.keys(assets).some(path => path.endsWith('.js')))
  console.log(JSON.stringify({ old_html: digest, assets, chromium: browser.version(), task: 'T-2', rendered: true }))
} finally {
  await browser.close()
}
