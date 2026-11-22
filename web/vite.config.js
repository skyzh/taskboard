import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'

const buildMeta = '<meta name="tasks-frontend-build" content="">'

export default {
  plugins: [{
    name: 'taskboard-frontend-build',
    apply: 'build',
    generateBundle: {
      order: 'post',
      handler(_options, bundle) {
        const index = bundle['index.html']
        if (index?.type !== 'asset') return
        const html = Buffer.from(index.source).toString()
        // The built index names the content-hashed bundles. The pre-paint
        // theme script is copied as a public asset, so include its bytes too.
        const theme = readFileSync(new URL('./public/assets/theme.js', import.meta.url))
        const build = createHash('sha256').update(html).update('\0').update(theme).digest('hex')
        index.source = html.replace(buildMeta, `<meta name="tasks-frontend-build" content="${build}">`)
      },
    },
  }],
}
