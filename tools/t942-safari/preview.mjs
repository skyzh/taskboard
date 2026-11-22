import { readFile, rename, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { extname, join } from 'node:path'

const root = process.argv[2]
const read = file => readFile(new URL(file, import.meta.url))
const api = JSON.parse(await read('synthetic-api.json'))
const measure = await read('measure.js')
const index = (await readFile('web/dist/index.html', 'utf8'))
  .replace('</body>', '<script defer src="/tasks/__measure.js"></script></body>')
const contentTypes = new Map([
  ['.css', 'text/css'],
  ['.js', 'text/javascript'],
  ['.png', 'image/png'],
])
const assets = new Map()
for (const entry of (await read('frontend.sha256')).toString().trim().split('\n')) {
  const [, file] = entry.split(/ {2}/)
  const type = contentTypes.get(extname(file))
  if (type) assets.set(file.replace('web/dist/', '/tasks/'), [type, await readFile(file)])
}

const server = createServer(async (request, response) => {
  const { method, url } = request
  const send = (status, type, body) => {
    console.log(method, url, status)
    response.writeHead(status, { 'Content-Type': type })
    response.end(body)
  }
  try {
    if (method === 'POST' && url === '/tasks/__measure') {
      const chunks = []
      for await (const chunk of request) chunks.push(chunk)
      const path = join(root, 'evidence', 'safari-metrics.json')
      await writeFile(path + '.tmp', Buffer.concat(chunks))
      await rename(path + '.tmp', path)
      return send(204, 'text/plain')
    }
    if (method === 'GET') {
      if (url === '/tasks/') return send(200, 'text/html', index)
      if (url === '/tasks/__measure.js') return send(200, 'text/javascript', measure)
      if (Object.hasOwn(api, url)) return send(200, 'application/json', JSON.stringify(api[url]))
      if (assets.has(url)) return send(200, ...assets.get(url))
    }
    return send(404, 'text/plain', 'Preview route not found')
  } catch (error) {
    console.error(error)
    send(500, 'text/plain', 'Preview error')
  }
})
server.listen(0, '127.0.0.1', async () => {
  const url = `http://127.0.0.1:${server.address().port}/tasks/#task-T-1`
  await writeFile(join(root, 'url'), url)
})
