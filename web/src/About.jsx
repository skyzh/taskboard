import React, { useState } from 'react'
import { useResource } from './data.js'
import { ResourceStatus, Timestamp } from './shared.jsx'

const gitCommit = /^[a-f0-9]{40}$/i
const utcTimestamp = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|\+00:00)$/

function ReleaseTime({ value }) {
  if (typeof value !== 'string' || !utcTimestamp.test(value) || !Number.isFinite(Date.parse(value))) {
    return <span className="about-unavailable">Unavailable</span>
  }
  return (
    <>
      <Timestamp at={value} />
      <code className="about-utc">{value}</code>
    </>
  )
}

function ReleaseCommit({ commit }) {
  const [copyStatus, setCopyStatus] = useState('')

  async function copy() {
    try {
      await navigator.clipboard.writeText(commit)
      setCopyStatus('Commit copied.')
    } catch {
      setCopyStatus('Select the commit text to copy it.')
    }
  }

  return (
    <>
      <a href={`https://github.com/skyzh/taskboard/commit/${commit}`}><code>{commit}</code></a>
      <button type="button" className="about-copy" onClick={copy}>Copy commit</button>
      <span className="about-copy-status" role="status">{copyStatus}</span>
    </>
  )
}

export default function About({ revision }) {
  const release = useResource('/release', revision)
  const metadata = release.data
  const commit = typeof metadata?.commit === 'string' && gitCommit.test(metadata.commit) ? metadata.commit : null

  return (
    <section className="about" aria-labelledby="about">
      <div className="page-heading"><h1 id="about">About</h1></div>
      <div className="panel about-panel">
        {release.error?.status === 404 ? <p>Release information is unavailable on this installation.</p>
          : <ResourceStatus resource={release} label="release information" />}
        {metadata && (
          <dl className="about-release">
            <dt>Git commit</dt>
            <dd>{commit ? <ReleaseCommit key={commit} commit={commit} /> : <span className="about-unavailable">Unavailable</span>}</dd>
            <dt>Built at</dt>
            <dd><ReleaseTime value={metadata?.built_at} /></dd>
            <dt>Deployed at</dt>
            <dd><ReleaseTime value={metadata?.deployed_at} /></dd>
          </dl>
        )}
      </div>
    </section>
  )
}
