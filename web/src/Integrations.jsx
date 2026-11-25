import React, { useRef, useState } from 'react'
import { ArrowRight } from 'lucide-react'
import taskSessionPrompt from '../../docs/taskboard/prompts/v1/AGENTS.md?raw'
import handoffPrompt from '../../docs/taskboard/prompts/v1/automated-handoff.md?raw'
import factoryPrompt from '../../docs/taskboard/prompts/v1/ai-factory.md?raw'
import localDriPrompt from '../../docs/taskboard/prompts/v1/local-dri.md?raw'
import teamDriPrompt from '../../docs/taskboard/prompts/v1/team-dri.md?raw'
import IntegrationDiagram from './IntegrationDiagram.jsx'
import { request, useResource } from './data.js'
import { CopyPrompt } from './PersonalCodex.jsx'
import { ResourceStatus, signInHref } from './shared.jsx'

const docs = 'https://github.com/skyzh/taskboard/blob/main/docs/taskboard/'
const guide = docs + 'PERSONAL-AGENTS.md'

const instructionsPath = '/personal-agents/publishing-instructions'
const registrationsPath = '/personal-agents/local-dris'
const instructionLimit = 8000

const workflows = [
  {
    id: 'task-session', label: 'Task sessions', file: 'AGENTS.md', prompt: taskSessionPrompt,
    description: 'Choose this for work you start and own. The session reads the ticket and Knowledge and reports under your name. You start each session.',
    setup: 'Copy the prompt for a session or add the AGENTS.md example to your repository. On an enabled trusted tailnet, use direct HTTPS. For signed access, get the Tasks CLI from your team’s approved checkout or source archive. No background process is needed.',
    guidance: 'Verify the human profile and publishing scope, find the original ticket, and read Knowledge and report progress as you work.',
    verify: 'Check your identity and one existing ticket, then record a checkpoint. Confirm it in Activity and open its related Knowledge from a fresh session.',
  },
  {
    id: 'automated-handoff', label: 'Automated handoffs', file: 'automated-handoff.md', prompt: handoffPrompt,
    description: 'Choose this to send a Ready ask to an agent without starting its session yourself. The managed watcher wakes the enrolled Raft agent, which reports on the ask under its own identity.',
    setup: 'On a Raft Computer, get Python 3.11 or later, the Raft CLI, and the reviewed Taskboard checkout. Use the managed watcher setup in the guide.',
    guidance: 'Verify the recipient and watcher, then open the linked ask and record Start and the result or blocker there.',
    verify: 'Send one linked ask and confirm the wake, Start, result, and acknowledgment.',
  },
  {
    id: 'ai-factory', label: 'Existing AI factory', file: 'ai-factory.md', prompt: factoryPrompt,
    description: 'Choose this if you already have a coordinator and workers. The coordinator owns the ticket and reports their results under its enrolled identity. Add a watcher if it needs to pick up work unattended.',
    setup: 'Have the project manager and board operator enroll an eligible coordinator. Use the shared CLI or supported Raft MCP adapter in the existing factory runtime. Other identities need an external adapter.',
    guidance: 'Verify the coordinator identity, read the assigned ticket and Knowledge, and report worker evidence under that identity.',
    verify: 'Assign the coordinator one small ticket and confirm its identity, worker evidence, and next owner in Activity.',
  },
  {
    id: 'local-dri', label: 'Local DRI', file: 'local-dri.md', prompt: localDriPrompt,
    description: 'Choose this for a named delegate with its own queue. It owns and reports work under its own name in your runtime. Start assigned work yourself, or run a watcher to receive asks from other agents.',
    setup: 'Until registration is available, use Task sessions with approved human access. A registered Local DRI can start an assigned ticket locally. Incoming asks from agents require a running watcher.',
    guidance: 'Confirm assignment, reopen unfinished work, report progress, and use the configured question route.',
    verify: 'Check the assigned owner before Start. Reopen unfinished work in the next session and check Activity. In progress is the last reported state.',
  },
  {
    id: 'team-dri', label: 'Team DRI', file: 'team-dri.md', prompt: teamDriPrompt,
    description: 'Choose this for ongoing ownership of a shared project area. A project manager assigns work to an eligible Team DRI, which reports under its own identity. Ready wakes can notify it about new work.',
    setup: 'Set up signed access in the agent runtime. Have a project manager confirm the agent is eligible and assign it a ticket. Project Ready wakes are optional.',
    guidance: 'Verify the team agent identity, work assigned tickets, route decisions, and report outcomes and evidence.',
    verify: 'Check the recorded owner and have a teammate find the blocker or result, source decisions, and evidence in Reports and Activity.',
  },
]

function WorkflowPrompt({ text, session }) {
  const human = session.data?.actor?.kind === 'human' && !session.error
  const resource = useResource(human ? instructionsPath : null)
  const [confirmed, setConfirmed] = useState(null)
  const [draft, setDraft] = useState(null)
  const [saving, setSaving] = useState(false)
  const [feedback, setFeedback] = useState(null)
  const editor = useRef(null)
  const saved = confirmed ?? resource.data?.instructions ?? ''
  const instructions = draft ?? saved
  const outdated = resource.error?.status === 404
  const canSave = human && !outdated
  const tooLong = canSave && Array.from(instructions).length > instructionLimit
  const prefix = instructions.trim()
  const prompt = text && (prefix
    ? `Apply these publishing instructions before writing to Taskboard. They take precedence over the workflow below. Continue excluded work privately and omit its tickets, updates, and evidence links from Taskboard.\n\n${prefix}\n\n${text}`
    : text)

  async function save(value, message, button) {
    setSaving(true)
    setFeedback(null)
    try {
      const result = await request(instructionsPath, { method: 'PUT', body: { instructions: value } })
      setConfirmed(result.instructions)
      setDraft(null)
      setFeedback({ text: message })
      if (document.activeElement === button || document.activeElement === document.body) {
        editor.current?.focus({ preventScroll: true })
      }
    } catch (error) {
      setFeedback({ text: error.message, error: true })
    } finally {
      setSaving(false)
    }
  }

  if (session.pending && !session.data) return <ResourceStatus resource={session} label="your session" />
  if (resource.pending || (resource.error && !outdated)) {
    return <ResourceStatus resource={resource} label="publishing instructions" />
  }

  return (
    <>
      <label className="integration-publishing-instructions">
        Publishing instructions (optional)
        <textarea ref={editor} rows={3} value={instructions} readOnly={saving}
          aria-describedby="publishing-instructions-help"
          aria-invalid={tooLong || undefined}
          data-reload-blocked={saving || (draft !== null && draft !== saved)}
          placeholder="For example, keep security-related work outside Taskboard."
          onChange={event => { setDraft(event.target.value); setFeedback(null) }} />
      </label>
      <p id="publishing-instructions-help">
        {outdated ? 'Update this board to save instructions across sessions.'
          : human ? 'Saved instructions are available the next time you sign in.'
            : session.data?.sign_in_available
              ? <><a href={signInHref(window.location.hash || '#integrations')}>Sign in</a> to save instructions across sessions.</>
              : 'Instructions entered here are included in the copied prompt for this page.'}
      </p>
      {canSave && (
        <div className="integration-instruction-actions">
          <button type="button" disabled={saving || instructions === saved || tooLong}
            onClick={event => save(instructions, 'Publishing instructions saved.', event.currentTarget)}>Save instructions</button>
          {saved && <button type="button" disabled={saving}
            onClick={event => save('', 'Saved publishing instructions cleared.', event.currentTarget)}>Clear saved instructions</button>}
        </div>
      )}
      {tooLong && <p role="alert">Use 8,000 characters or fewer to save these instructions.</p>}
      {feedback && <p role={feedback.error ? 'alert' : 'status'}>{feedback.text}</p>}
      {prompt && <CopyPrompt key={prompt} prompt={prompt} />}
    </>
  )
}

function LocalRegistrationForm({ projects, project, reload }) {
  const [name, setName] = useState('')
  const [scope, setScope] = useState(project || '')
  const [saving, setSaving] = useState(false)
  const [feedback, setFeedback] = useState(null)
  const canRegister = name.trim() && projects.some(item => item.prefix === scope)

  async function register(event) {
    event.preventDefault()
    if (saving || !canRegister) return
    setSaving(true)
    setFeedback(null)
    try {
      await request(registrationsPath, { body: { name: name.trim(), project: scope } })
      setName('')
      setFeedback({ text: 'Local DRI registered. Choose it as the reporting identity for local work to get its prompt.' })
      reload()
    } catch (error) {
      setFeedback({ error: true, text: error.message })
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="profile-form integration-registration-form" id="local-dri-registration"
      data-reload-blocked={Boolean(saving || name)} onSubmit={register}>
      <label>Name<input value={name} maxLength={80} onChange={event => setName(event.currentTarget.value)} required /></label>
      <label>
        Project for the Local DRI
        <select value={scope} onChange={event => setScope(event.currentTarget.value)} required>
          <option value="">Choose a project</option>
          {projects.map(item => <option key={item.prefix} value={item.prefix}>{item.title}</option>)}
        </select>
      </label>
      <button type="submit" disabled={saving || !canRegister}>{saving ? 'Registering…' : 'Register'}</button>
      {feedback && <p role={feedback.error ? 'alert' : 'status'}>{feedback.text}</p>}
    </form>
  )
}

function WorkflowGuide({ board, project, projects, onProjectChange, session, access, revision }) {
  const actor = session.error ? null : session.data?.actor
  const human = actor?.kind === 'human'
  const [selected, setSelected] = useState(workflows[0].id)
  const [identityId, setIdentityId] = useState('')
  const [registrationOpen, setRegistrationOpen] = useState(false)
  const [savingDestination, setSavingDestination] = useState(false)
  const [destinationFeedback, setDestinationFeedback] = useState(null)
  const registrationResource = useResource(human ? registrationsPath : null, revision)
  const registrations = registrationResource.data?.local_dris || []
  const identity = registrations.find(item => item.id === identityId)
  const unavailableIdentity = Boolean(identityId && !identity)
  const destination = identity?.human_questions
  const supportsDestination = destination === 'local_prompt' || destination === 'taskboard'
  const destinationPending = savingDestination || registrationResource.pending
  const promptProject = identity?.project || (unavailableIdentity ? undefined : project)
  const workflow = workflows.find(item => item.id === selected)
  const questionInstructions = destination === 'taskboard'
    ? `For a Decision on an owned In progress ticket, use \`tasks --shared --url ${board} --local-dri ${identity.id} assign-human TICKET "Question"\`. Omit \`--on\` for your creator or add \`--on PERSON\` for another eligible human. With signed HTTP, use \`advance-task\` with \`transition: assign-human\` and optional \`on\` or \`on_name\`. For a nonblocking request, use signed \`raise-attention\` with the ticket, reason, and optional recipient ID. Read a Decision answer when the ticket returns to Ready.`
    : 'Ask your creator through the runtime’s local question prompt.'
  const versionedPrompt = workflow.prompt.trim().replace('<BOARD_HTTPS_URL>', () => board)
    .replace('<PROJECT>', promptProject || '<PROJECT>')
    .replace('<ACTION_MANIFEST_URL>', () => new URL('.well-known/raft-agent-manifest.json', board).href)
    .replace('<QUESTION_INSTRUCTIONS>', () => questionInstructions)
  const identityPrompt = identity && [
    `Use the verified Local DRI "${identity.name}" for project ${identity.project}. Its ID is ${identity.id}.`,
    `Verify it with tasks --shared --url ${board} --local-dri ${identity.id} context before writing.`,
  ].join(' ')
  const text = identityId && (unavailableIdentity || destinationPending || registrationResource.error)
    ? null : identityPrompt ? `${identityPrompt}\n\n${versionedPrompt}` : versionedPrompt

  async function saveDestination(value) {
    setSavingDestination(true)
    setDestinationFeedback(null)
    try {
      await request(`${registrationsPath}/${encodeURIComponent(identity.id)}`, {
        method: 'PUT', body: { human_questions: value },
      })
      registrationResource.retry()
      setDestinationFeedback({ id: identity.id, text: 'Question destination saved.' })
    } catch (error) {
      setDestinationFeedback({ id: identity.id, text: error.message, error: true })
    } finally {
      setSavingDestination(false)
    }
  }
  return (
    <>
      <p className="integration-mode-example">
        For example, use <strong>Task sessions</strong> to fix a bug on your ticket and report it under your name.
        Choose <strong>Local DRI</strong> if you want a named code review delegate with its own queue.
        You can start the delegate yourself or set up a watcher for unattended handoffs.
      </p>
      {human && (
        <div className="integration-identity">
          <label className="settings-preference">
            Reporting identity for local work
            <select value={identityId} onChange={event => {
              const id = event.currentTarget.value
              setIdentityId(id)
              setSelected(id ? 'local-dri' : 'task-session')
            }}>
              <option value="">Human</option>
              {unavailableIdentity && <option value={identityId} disabled>Selected Local DRI unavailable</option>}
              {registrations.map(item => <option key={item.id} value={item.id}>{item.name} · {item.project}</option>)}
            </select>
          </label>
          <p>{!identityId
            ? 'Human reports activity under your profile, with the time it was reported. Unattended agent asks go to an eligible DRI.'
            : registrationResource.pending ? 'Checking watcher readiness…'
              : registrationResource.error || unavailableIdentity ? 'The selected Local DRI could not be verified.'
                : identity.watcher_ready
                  ? 'At the last check, the watcher was ready for its configured requester in this project.'
                  : 'Set up the watcher before sending this Local DRI an ask from another agent.'}
          </p>
          {identity && <p className="integration-dri-id">Local DRI ID <code>{identity.id}</code></p>}
          {supportsDestination && (
            <>
              <label className="settings-preference">
                Where this Local DRI asks you
                <select value={destination} aria-describedby="question-destination-help"
                  disabled={destinationPending || Boolean(registrationResource.error)}
                  onChange={event => saveDestination(event.currentTarget.value)}>
                  <option value="local_prompt">In the local session</option>
                  <option value="taskboard">In Taskboard (Needs me)</option>
                </select>
              </label>
              <p id="question-destination-help">Taskboard questions go to the person who registered this Local DRI by default. The DRI can choose another eligible human.</p>
              {savingDestination && <p role="status">Saving question destination…</p>}
              {!destinationPending && destinationFeedback?.id === identity.id && !registrationResource.error &&
                <p role={destinationFeedback.error ? 'alert' : 'status'}>{destinationFeedback.text}</p>}
            </>
          )}
          {registrationResource.error?.status === 404
            ? <p className="integration-setup-note">Local DRI registration is pending on this board.</p>
            : <ResourceStatus resource={registrationResource} label="Local DRIs" />}
          {registrationResource.data && projects?.length > 0 && (
            <button type="button" aria-expanded={registrationOpen} aria-controls="local-dri-registration"
              onClick={() => setRegistrationOpen(value => !value)}>Register Local DRI</button>
          )}
          {registrationOpen && projects?.length > 0 &&
            <LocalRegistrationForm projects={projects} project={project} reload={registrationResource.retry} />}
        </div>
      )}
      <h3 id="agent-workflow">How do you use your agents?</h3>
      <ol className="integration-paths" aria-labelledby="agent-workflow" role="list">
        {workflows.map((item, index) => <li key={item.id}>
          {index > 0 && <ArrowRight className="integration-path-arrow" size={14} aria-hidden="true" />}
          <button type="button" aria-pressed={selected === item.id} aria-controls="agent-workflow-details"
            onClick={() => {
              setSelected(item.id)
              if (item.id !== 'local-dri') setIdentityId('')
            }}>{item.label}</button>
        </li>)}
      </ol>
      {projects?.length > 0 && (
        <label className="settings-preference integration-project">
          Project for the prompt
          <select value={promptProject || ''} disabled={Boolean(identityId)}
            onChange={event => onProjectChange(event.currentTarget.value)}>
            <option value="">Choose a project</option>
            {projects.map(item => <option key={item.prefix} value={item.prefix}>{item.title}</option>)}
          </select>
        </label>
      )}
      <section id="agent-workflow-details" aria-labelledby="agent-workflow-path">
        <h3 id="agent-workflow-path">{workflow.label}</h3>
        <p>{workflow.description}</p>
        <IntegrationDiagram mode={workflow.id} destination={destination}
          localRegistrationUnavailable={registrationResource.error?.status === 404} />
        {workflow.id === 'team-dri' &&
          <p className="integration-setup-note">Raft agents register as Team DRIs when they first connect to Taskboard.</p>}
        <dl className="integration-path-details">
          <dt>Set up</dt>
          <dd>{workflow.id === 'local-dri' && registrationResource.data
            ? <>Use the approved Tasks CLI to sign in, then pass your registered ID with <code>--local-dri</code>.
              Follow the <a href={docs + 'LOCAL-DRI.md'}>Local DRI setup and stop steps</a>.</>
            : workflow.setup}</dd>
          {workflow.id === 'automated-handoff' && registrationResource.data && <>
            <dt>Local trial</dt>
            <dd>Install the <a href={docs + 'LOCAL-HANDOFF-WATCHER.md'}>local handoff watcher</a> and start it before sending a Ready ask.
              Stop it when finished and check that readiness clears.</dd>
          </>}
          <dt>Verify</dt>
          <dd>{workflow.verify}</dd>
        </dl>
        {workflow.id === 'task-session' && (access.data?.mode === 'tailnet'
          ? <p>This device can use the board’s trusted-tailnet access.</p>
          : access.data?.mode === 'login_required' && <>
            <p>To sign in, run this command and approve it in your browser.</p>
            <p className="cli-login-command"><code>tasks --shared --url {board} login</code></p>
          </>)}
        <p className="integration-guide-link">
          <a href={`${guide}#${workflow.id}`}>Read the setup guide</a>
          <a href={`${docs}prompts/v1/${workflow.file}`}>Open v1 prompt file</a>
        </p>
        <section className="integration-prompt-section" aria-labelledby="integration-prompt">
          <h4 id="integration-prompt">What the prompt gives your agent</h4>
          <p>The copied prompt includes this board’s URL, {promptProject ? 'the selected project' : 'a project placeholder'},
            access guidance, and any publishing instructions you enter. {workflow.guidance}</p>
          {workflow.id === 'local-dri' && identity &&
            <p>The prompt includes the selected Local DRI identity and its question route.</p>}
          <p>Copying the prompt supplies instructions. Complete any sign-in, enrollment, adapter, or watcher setup required for this mode separately.</p>
          {!promptProject && !unavailableIdentity && <p>Replace <code>&lt;PROJECT&gt;</code> with your project prefix before using the prompt.</p>}
          <WorkflowPrompt key={actor?.id ?? 'unsigned'} text={text} session={session} />
        </section>
      </section>
    </>
  )
}

function CliSignIn({ challenge, session }) {
  const [pending, setPending] = useState(false)
  const [approved, setApproved] = useState(false)
  const [error, setError] = useState(null)
  const human = session.data?.actor?.kind === 'human'
  const signIn = session.data?.sign_in_available || session.error?.status === 401

  async function approve() {
    setPending(true)
    setError(null)
    try {
      await request('/personal-agents/cli-authorize', { body: { challenge } })
      setApproved(true)
    } catch (failure) {
      setError(failure.message)
    } finally {
      setPending(false)
    }
  }

  return (
    <section className="panel settings-panel" aria-labelledby="cli-sign-in">
      <h2 id="cli-sign-in">Approve CLI sign-in</h2>
      {approved ? <p role="status">Approved. Return to the terminal to finish signing in.</p> : <>
        <p>Make sure this code matches your terminal before approving.</p>
        <p className="cli-sign-in-code"><code>{challenge.slice(0, 8).toUpperCase()}</code></p>
        {human ? <button type="button" disabled={pending} onClick={approve}>
          {pending ? 'Approving…' : 'Approve sign-in'}
        </button> : signIn ? <a href={signInHref(`#integrations?cli=${challenge}`)}>Sign in to approve</a>
          : session.pending ? <ResourceStatus resource={session} label="your session" />
            : <p>A signed-in person must approve this request.</p>}
        {error && <p role="alert">{error}</p>}
      </>}
    </section>
  )
}

export default function Integrations({ session, project, projects, onProjectChange, revision, challenge = null, preview = false }) {
  const access = useResource(challenge === null ? '/personal-agents/access' : null)
  const board = preview ? 'https://board.example/tasks/' : new URL('./', window.location.href).href
  return (
    <section className={challenge === null ? 'settings integrations integrations-setup' : 'settings integrations'}
      aria-labelledby="integrations">
      <div className="page-heading"><h1 id="integrations" tabIndex={-1}>Integrations</h1></div>
      {challenge !== null ? /^[a-f0-9]{64}$/.test(challenge)
        ? <CliSignIn key={`${challenge}:${session.data?.actor?.id || ''}`} challenge={challenge} session={session} />
        : <p role="alert">This sign-in link is invalid. Start sign-in again from the terminal.</p>
        : <section className="panel settings-panel" aria-labelledby="personal-agents">
          <h2 id="personal-agents">Personal Agents</h2>
          <WorkflowGuide key={session.error ? 'unsigned' : session.data?.actor?.id ?? 'unsigned'}
            board={board} project={project} projects={projects} onProjectChange={onProjectChange}
            session={session} access={access} revision={revision} />
        </section>}
    </section>
  )
}
