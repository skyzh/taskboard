import { readdir, readFile, rename, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { extname, join } from 'node:path'

const [dist, evidence] = process.argv.slice(2)
const read = file => readFile(new URL(file, import.meta.url), 'utf8')
const original = await readFile(join(dist, 'index.html'), 'utf8')
const api = JSON.parse(await read('synthetic-api.json'))
const build = original.match(/name="tasks-frontend-build" content="([a-f0-9]{64})"/)?.[1]
if (!build) throw new Error('Missing stamped frontend build')
api['/tasks/api/v1/frontend-build'] = {build}
api['/tasks/api/v1/reports?view=active&project=T&limit=20'] = api['/tasks/api/v1/reports?project=T&limit=20']
const measure = await read('measure.js')
const capture = `
(async () => {
  const layout = new URL(location.href).searchParams.get('layout');
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  try {
    const rows = '.report-task-summary .task-row-heading > .report-status';
    for (let attempt = 0; attempt < 240 && document.querySelectorAll(rows).length < 2; attempt++) await pause(200);
    if (document.querySelectorAll(rows).length < 2) return;
    await document.fonts.ready;
    document.querySelector('.report-waiting-line')?.closest('.report-owner-section')?.scrollIntoView({block:'start'});
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const data = sampleRows();
    await fetch('/tasks/__measure/' + layout, {method:'POST', body:JSON.stringify(data, null, 2)});
  } catch (error) {
    await fetch('/tasks/__measure/' + layout, {method:'POST', body:JSON.stringify({error:String(error)})});
  }
})();`
const contentTypes = {'.css':'text/css', '.js':'text/javascript', '.png':'image/png'}
const assets = new Map()
for (const file of await readdir(join(dist, 'assets'))) {
  const type = contentTypes[extname(file)]
  if (type) assets.set('/tasks/assets/' + file, [type, await readFile(join(dist, 'assets', file))])
}
const server = createServer(async (request, response) => {
  const {method, url: path} = request
  const url = new URL(path, 'http://127.0.0.1')
  const send = (status, type, body) => {
    response.writeHead(status, {'Content-Type':type, 'Cache-Control':'no-store'})
    response.end(body)
  }
  try {
    const metric = url.pathname.match(/^\/tasks\/__measure\/(compact|wide|desktop)$/)
    if (method === 'POST' && metric) {
      const chunks = []
      for await (const chunk of request) chunks.push(chunk)
      const file = join(evidence, metric[1] + '.json')
      await writeFile(file + '.tmp', Buffer.concat(chunks))
      await rename(file + '.tmp', file)
      return send(204, 'text/plain')
    }
    if (method === 'GET') {
      if (url.pathname === '/tasks/') {
        const index = url.searchParams.get('layout') === 'wide'
          ? original.replace('width=device-width, initial-scale=1', 'width=768') : original
        return send(200, 'text/html', index.replace('</body>', '<script defer src="/tasks/__measure.js"></script></body>'))
      }
      if (path === '/tasks/__measure.js') return send(200, 'text/javascript', measure + capture)
      if (Object.hasOwn(api, path)) return send(200, 'application/json', JSON.stringify(api[path]))
      if (assets.has(path)) return send(200, ...assets.get(path))
    }
    return send(404, 'text/plain', 'Preview route not found')
  } catch (error) {
    console.error(error)
    return send(500, 'text/plain', 'Preview error')
  }
})
server.listen(0, '127.0.0.1', async () => {
  await writeFile(join(evidence, 'url'), `http://127.0.0.1:${server.address().port}/tasks/`)
})
