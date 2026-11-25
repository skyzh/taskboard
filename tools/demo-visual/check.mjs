import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFile, mkdir, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { createRequire } from 'node:module'
import { extname, resolve, sep } from 'node:path'
import { parseArgs } from 'node:util'

const require = createRequire(new URL('../../web/package.json', import.meta.url))
const { chromium, webkit } = require('playwright')
const options = {
  dist: { type: 'string' },
  baseline: { type: 'string' },
  output: { type: 'string' },
}
const clock = '2026-11-24T06:06:11Z'
const views = [
  { name: 'phone', engine: webkit, viewport: { width: 390, height: 850 }, isMobile: true, hasTouch: true },
  { name: 'desktop', engine: chromium, viewport: { width: 1280, height: 900 } },
]
const tourRoutes = [
  '/reports', '/reports', '/tickets/T-1406',
  ...Array(4).fill('/activity?detail=all'),
  ...Array(2).fill('/reports?project=FLOW'),
  ...Array(2).fill('/integrations?project=T'),
]
const contentTypes = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
  '.png': 'image/png', '.svg': 'image/svg+xml',
}
const digest = bytes => createHash('sha256').update(bytes).digest('hex')

async function serveDemo(dist) {
  const directory = resolve(dist)
  await readFile(resolve(directory, 'index.html'))
  const mount = '/preview/'
  const server = createServer(async (request, response) => {
    const pathname = new URL(request.url, 'http://127.0.0.1').pathname
    const file = resolve(directory, pathname === mount ? 'index.html' : pathname.slice(mount.length))
    if (request.method !== 'GET' || !pathname.startsWith(mount) || !file.startsWith(directory + sep)) {
      response.writeHead(404).end()
      return
    }
    try {
      const content = await readFile(file)
      response.writeHead(200, { 'Content-Type': contentTypes[extname(file)] || 'application/octet-stream' }).end(content)
    } catch (error) {
      response.writeHead(['ENOENT', 'EISDIR'].includes(error.code) ? 404 : 500).end()
    }
  })
  await new Promise((listening, reject) => server.once('error', reject).listen(0, '127.0.0.1', listening))
  return { server, pageURL: `http://127.0.0.1:${server.address().port}${mount}` }
}

async function capture(pageURL, directory) {
  await mkdir(directory, { recursive: true })
  const result = { schema: 1, clock, browsers: {}, screens: {}, interaction_failures: [] }
  for (const { name, engine, ...contextOptions } of views) {
    const browser = await engine.launch()
    try {
      result.browsers[name] = { name: engine.name(), version: browser.version(), ...contextOptions }
      const context = await browser.newContext({
        ...contextOptions, locale: 'en-US', timezoneId: 'UTC', colorScheme: 'light',
        reducedMotion: 'reduce', deviceScaleFactor: 1, serviceWorkers: 'block',
      })
      const unexpected = []
      await context.route('**/*', route => {
        const { pathname } = new URL(route.request().url())
        if (route.request().url().startsWith(pageURL) && !pathname.includes('/api/') && route.request().method() === 'GET') {
          return route.continue()
        }
        unexpected.push(route.request().method() + ' ' + pathname)
        return route.abort()
      })
      const page = await context.newPage()
      page.setDefaultTimeout(5000)
      await page.clock.setFixedTime(new Date(clock))
      let step = 'reports'
      const go = async hash => {
        const response = await page.goto(pageURL + hash)
        if (response) assert.equal(response.status(), 200, 'Static document failed to load')
      }
      const screenshot = async (key, subject = page) => {
        await page.mouse.move(0, 0)
        await page.evaluate(async () => {
          await document.fonts.ready
          await new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))
        })
        const file = `${name}-${key}.png`
        const bytes = await subject.screenshot({ animations: 'disabled', caret: 'hide' })
        await writeFile(resolve(directory, file), bytes)
        result.screens[file] = digest(bytes)
      }
      const panel = page.locator('.demo-tour')
      const launcher = page.getByRole('button', { name: 'Tour', exact: true })
      const highlighted = page.locator('[data-tour-target]')
      const reports = page.getByRole('heading', { name: 'Reports', exact: true })
      const returnedFocus = async () => {
        await panel.waitFor({ state: 'detached' })
        await page.waitForFunction(() => document.activeElement?.matches('button.demo-tour-toggle'))
      }
      try {
        await go('#/reports')
        await reports.waitFor()
        await page.locator('.report-owner').first().waitFor()
        await screenshot('reports')

        step = 'ticket bookmark and reload'
        await page.goto('about:blank')
        await go('#task-T-1406')
        const ticket = page.getByRole('heading', { name: 'Open All projects from the Reports home URL' })
        await ticket.waitFor()
        assert.equal(new URL(page.url()).hash, '#/tickets/T-1406')
        await page.reload()
        await ticket.waitFor()
        await screenshot('ticket')

        step = 'activity bookmark and focus'
        await go('#/activity/events/4')
        await page.locator('li#event-4-log').waitFor()
        await page.waitForFunction(() => document.activeElement?.id === 'event-4-log')
        await screenshot('activity')

        step = 'development workflow link'
        await go('#/reports')
        await page.getByRole('link', { name: 'Open the development workflow' }).click()
        await page.waitForURL(url => url.hash === '#/reports?project=FLOW')
        await page.getByRole('link', { name: /FLOW-4/ }).first().waitFor()
        await screenshot('workflow')
        const blocker = page.locator('.task-row').filter({ hasText: /navigation notes/i })
          .getByRole('link', { name: 'FLOW-1', exact: true })
        await blocker.click()
        await page.waitForURL(url => url.hash === '#/tickets/FLOW-1')
        await page.getByRole('navigation', { name: 'Taskboard views' }).getByRole('link', { name: 'Tasks', exact: true }).click()
        await page.waitForURL(url => url.hash.startsWith('#/tasks?') && new URLSearchParams(url.hash.split('?')[1]).get('project') === 'FLOW')

        step = 'read-only demo action'
        await go('#/tickets/FLOW-1')
        await page.getByRole('button', { name: 'Edit ticket', exact: true }).click()
        const details = page.locator('details.ticket-context')
        if ((await details.count()) && (await details.getAttribute('open')) === null) {
          await details.locator(':scope > summary').click()
        }
        const watch = page.getByRole('button', { name: 'Watch task', exact: true })
        await watch.click()
        await page.getByText('This action requires the full board.', { exact: true }).waitFor()
        assert.equal(await watch.getAttribute('aria-pressed'), 'false')

        step = 'Activity link, Back and Forward'
        await go('#/reports')
        await page.getByRole('link', { name: 'Activity by Gauge', exact: true }).click()
        await page.locator('li#event-4-log').waitFor()
        await page.goBack()
        await reports.waitFor()
        await page.goForward()
        await page.locator('li#event-4-log').waitFor()

        step = 'launch tour'
        await go('#/reports')
        await reports.waitFor()
        await launcher.press('Enter')
        const heading = panel.getByRole('heading')
        await heading.waitFor()
        const count = Number((await heading.innerText()).match(/\b1 of (\d+)$/)?.[1])
        assert.equal(count, tourRoutes.length, 'Tour step count changed')
        for (let index = 1; index <= count; index++) {
          step = `tour step ${index}`
          await panel.getByRole('heading', { name: new RegExp(` ${index} of ${count}$`) }).waitFor()
          const active = new URL(page.url()).hash.slice(1)
          const [route, query] = active.split('?', 2)
          const filters = new URLSearchParams(query)
          filters.delete('tour')
          assert.equal(route + (filters.size ? '?' + filters : ''), tourRoutes[index - 1], 'Unexpected tour route')
          await highlighted.waitFor()
          const outline = await highlighted.evaluate(node => {
            const style = getComputedStyle(node)
            return style.outlineStyle !== 'none' && Number.parseFloat(style.outlineWidth) > 0
          })
          assert.ok(outline, 'The declared tour target has no visible highlight')
          assert.equal(await highlighted.count(), 1, 'Expected one visible highlighted target')
          await page.waitForFunction(() => {
            const target = document.querySelector('[data-tour-target]')?.getBoundingClientRect()
            return target && target.top < innerHeight && target.bottom > 0
          })
          await screenshot(`tour-${index}`, highlighted)
          await panel.getByRole('button', { name: index === count ? 'Finish' : 'Next', exact: true }).press('Enter')
        }

        step = 'focus after Finish'
        await returnedFocus()

        for (const exit of ['Escape', 'Close tour']) {
          step = 'focus after ' + exit
          await go('#/reports')
          await reports.waitFor()
          await launcher.press('Enter')
          for (let index = 1; index <= 2; index++) {
            await panel.getByRole('heading', { name: new RegExp(` ${index} of ${count}$`) }).waitFor()
            await panel.getByRole('button', { name: 'Next', exact: true }).press('Enter')
          }
          await page.waitForURL(url => url.hash.startsWith('#/tickets/T-1406'))
          await panel.getByRole('heading', { name: new RegExp(` 3 of ${count}$`) }).waitFor()
          if (exit === 'Escape') await panel.press('Escape')
          else await panel.getByRole('button', { name: exit, exact: true }).press('Enter')
          await returnedFocus()
        }
        step = 'static demo requests'
        assert.deepEqual(unexpected, [], 'The demo requested a write, API, or external resource')
      } catch (error) {
        if (page.isClosed()) throw error
        await screenshot('failure')
        result.interaction_failures.push({
          view: name, step, route: new URL(page.url()).hash,
          message: error.message.split('\n', 1)[0],
        })
      }
    } finally {
      await browser.close()
    }
  }
  return result
}

async function baselineAt(directory) {
  const baseline = JSON.parse(await readFile(resolve(directory, 'result.json'), 'utf8'))
  assert.equal(baseline.schema, 1, 'Unsupported comparison baseline')
  assert.equal(baseline.status, 'captured', 'Generate an approved baseline in capture-only mode')
  assert.ok(Object.keys(baseline.screens).length, 'Approved baseline has no screenshots')
  for (const [file, hash] of Object.entries(baseline.screens)) {
    assert.match(file, /^(phone|desktop)-[a-z0-9-]+\.png$/)
    assert.equal(digest(await readFile(resolve(directory, file))), hash, `Baseline screenshot checksum failed for ${file}`)
  }
  return baseline
}

function compare(candidate, baseline) {
  const candidates = new Set([...Object.keys(candidate.screens), ...Object.keys(baseline.screens)])
  return [...candidates].filter(file => candidate.screens[file] !== baseline.screens[file]).sort()
}

const { values } = parseArgs({ options })
assert.ok(values.dist && values.output, 'Pass --dist and --output, and optionally --baseline')
const output = resolve(values.output)
await mkdir(output, { recursive: true })
let server
let result
try {
  if (values.baseline) assert.notEqual(resolve(values.baseline), output, 'Use separate baseline and evidence directories')
  const baseline = values.baseline && await baselineAt(values.baseline)
  const published = await serveDemo(values.dist)
  server = published.server
  const candidate = await capture(published.pageURL, output)
  result = { ...candidate, changed_screens: [], status: 'captured' }
  if (baseline) {
    assert.equal(candidate.clock, baseline.clock, 'Fixed clock differs from the baseline')
    assert.deepEqual(candidate.browsers, baseline.browsers, 'Browser runtime differs from the baseline')
    if (!candidate.interaction_failures.length) result.changed_screens = compare(candidate, baseline)
    result.status = 'pass'
  }
  if (result.changed_screens.length || candidate.interaction_failures.length) {
    const repeat = await capture(published.pageURL, resolve(output, 'repeat'))
    const stability = evidence => JSON.stringify(evidence.interaction_failures.map(({ view, step, route }) => ({ view, step, route })))
    assert.ok(stability(candidate) === stability(repeat), 'Interactions varied between the two runs')
    if (!candidate.interaction_failures.length) {
      const unstable = compare(candidate, repeat)
      assert.ok(!unstable.length, `Screenshots varied between the two runs: ${unstable.join(', ')}`)
    }
    result.status = 'changed'
    process.exitCode = 2
  }
} catch (error) {
  result = { ...result, status: 'error', error: error.message,
    changed_screens: result?.changed_screens || [], interaction_failures: result?.interaction_failures || [] }
  process.exitCode = 1
} finally {
  if (server) await new Promise(done => server.close(done))
}

await writeFile(resolve(output, 'result.json'), JSON.stringify(result, null, 2) + '\n')
console.log(JSON.stringify({ status: result.status, changed_screens: result.changed_screens,
  interaction_failures: result.interaction_failures, ...(result.error && { error: result.error }) }))
