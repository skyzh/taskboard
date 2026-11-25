import React, { useCallback, useEffect, useId, useRef, useState } from 'react'
import { Monitor, Moon, Sun } from 'lucide-react'
import { request, signOut } from './data.js'
import { AssigneeAvatar, ResourceStatus, signInHref } from './shared.jsx'
import { matchesBoardShortcut } from './shortcuts.js'

export function ProfileForm({ actor, onSaved }) {
  const [role, setRole] = useState(actor.role || '')
  const [shortRole, setShortRole] = useState(actor.short_role || '')
  const [description, setDescription] = useState(actor.description || '')
  const [avatar, setAvatar] = useState(null)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState(false)
  const shortRoleHelp = useId()
  const updates = {}
  if (role.trim() !== (actor.role || '')) updates.role = role.trim()
  if (shortRole.trim() !== (actor.short_role || '')) updates.short_role = shortRole.trim()
  if (description.trim() !== (actor.description || '').trim()) updates.description = description.trim()
  const hasTextChanges = Object.keys(updates).length > 0

  async function save(event) {
    event.preventDefault()
    if (saving) return
    const form = event.currentTarget
    setSaving(true)
    setMessage('')
    setError(false)
    try {
      let saved
      if (avatar) {
        const body = new FormData()
        for (const [key, value] of Object.entries(updates)) body.set(key, value)
        body.set('avatar', avatar)
        saved = (await request('/profile/avatar', { body })).actor
        form.elements.avatar.value = ''
        setAvatar(null)
      } else {
        saved = (await request('/profile', { method: 'PATCH', body: updates })).actor
      }
      onSaved(saved)
      setMessage('Profile saved.')
    } catch (failure) {
      setMessage(failure.message)
      setError(true)
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="profile-form" onSubmit={save}
      data-reload-blocked={Boolean(saving || avatar || hasTextChanges)}>
      <label>Short role<input value={shortRole} maxLength={60} aria-describedby={shortRoleHelp}
        onChange={event => setShortRole(event.target.value)} /></label>
      <p id={shortRoleHelp} className="field-guidance-help">Use two or three words for Reports Summary, such as “Tiny LLM implementation.”</p>
      {actor.kind !== 'human' && <label>Role<input value={role} maxLength={100}
        onChange={event => setRole(event.target.value)} /></label>}
      <label>Description<textarea value={description} maxLength={2000} rows={3}
        onChange={event => setDescription(event.target.value)} /></label>
      <label>Avatar<input name="avatar" type="file" accept="image/png,image/jpeg,image/webp"
        onChange={event => setAvatar(event.target.files[0] || null)} /></label>
      <button disabled={saving || (!avatar && !hasTextChanges)}>
        {saving ? 'Saving…' : 'Save profile'}
      </button>
      {message && <p role={error ? 'alert' : 'status'}>{message}</p>}
    </form>
  )
}

export function ProfileSignIn({ session, fragment }) {
  const unauthorized = session.error?.status === 401
  if (!unauthorized && !session.data?.sign_in_available) return null
  return <a href={signInHref(fragment)}>Sign in with Raft</a>
}

const themeActions = [
  { value: '', label: 'Auto', Icon: Monitor },
  { value: 'day', label: 'Day', Icon: Sun },
  { value: 'night', label: 'Night', Icon: Moon },
]

export default function Profile({ session, fragment, theme, freshness, settingsHref = '#settings', preview = false }) {
  const actor = session.data?.actor
  const panelId = useId()
  const shortcutsTitle = useId()
  const profile = useRef(null)
  const trigger = useRef(null)
  const shortcuts = useRef(null)
  const shortcutsOpener = useRef(null)
  const [open, setOpen] = useState(false)
  const [signingOut, setSigningOut] = useState(false)
  const [signOutFailed, setSignOutFailed] = useState(false)
  const verified = actor?.kind === 'human' || actor?.kind === 'agent'
  const name = verified ? actor.name : 'Not signed in'

  const showShortcuts = useCallback(fromMenu => {
    const dialog = shortcuts.current
    if (!dialog || dialog.open) return
    shortcutsOpener.current = fromMenu ? trigger.current : document.activeElement
    setOpen(false)
    dialog.showModal()
  }, [])

  function restoreShortcutFocus() {
    const dialog = shortcuts.current
    if (!dialog || dialog.open) return
    const opener = shortcutsOpener.current
    shortcutsOpener.current = null
    if (document.activeElement === document.body || dialog.contains(document.activeElement)) {
      opener?.focus()
    }
  }

  useEffect(() => {
    function helpShortcut(event) {
      if (!shortcuts.current || !matchesBoardShortcut(event, 'KeyH')) return
      event.preventDefault()
      showShortcuts(false)
    }
    window.addEventListener('keydown', helpShortcut)
    return () => window.removeEventListener('keydown', helpShortcut)
  }, [showShortcuts])

  useEffect(() => {
    if (!open) return
    function close() { setOpen(false) }
    function outside(event) { if (!profile.current?.contains(event.target)) close() }
    function escape(event) {
      if (event.key !== 'Escape') return
      event.preventDefault()
      close()
      trigger.current?.focus()
    }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape)
    window.addEventListener('hashchange', close)
    return () => {
      document.removeEventListener('pointerdown', outside)
      document.removeEventListener('keydown', escape)
      window.removeEventListener('hashchange', close)
    }
  }, [open])

  if (session.pending && !actor) return null
  if (session.error && session.error.status !== 401) {
    return <ResourceStatus resource={session} label="your session" />
  }

  function closeAndFocus() {
    setOpen(false)
    trigger.current?.focus()
  }

  async function handleSignOut() {
    setSigningOut(true)
    setSignOutFailed(false)
    try {
      await signOut()
      window.location.reload()
    } catch {
      setSigningOut(false)
      setSignOutFailed(true)
    }
  }

  return (
    <div className="profile" ref={profile}
      onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false) }}>
      <button ref={trigger} type="button" className="profile-toggle" aria-expanded={open} aria-controls={panelId}
        aria-label={verified ? `${name}’s profile` : name} onClick={() => setOpen(value => !value)}>
        {verified && <AssigneeAvatar name={actor.name} url={actor.avatar_url} />}
        <span className="profile-name">{name}</span>
        <span className="profile-chevron" aria-hidden="true" />
      </button>
      <div id={panelId} className="profile-actions" hidden={!open}>
        <div className="profile-theme-switch" role="group" aria-label="Color theme">
          {themeActions.map(({ value, label, Icon }) => (
            <button key={label} type="button" aria-label={label} title={label}
              aria-pressed={theme.preference === value} onClick={() => theme.chooseTheme(value)}>
              <Icon size={18} aria-hidden="true" />
            </button>
          ))}
        </div>
        <a href={settingsHref} onClick={closeAndFocus}>Settings</a>
        {actor?.kind === 'human' && <a href="#integrations" onClick={closeAndFocus}>Integrations</a>}
        <a href="#about" onClick={closeAndFocus}>About</a>
        <button type="button" aria-keyshortcuts="Alt+Shift+H" onClick={() => showShortcuts(true)}>Keyboard shortcuts</button>
        <ProfileSignIn session={session} fragment={fragment} />
        {verified && !preview && <button type="button" onClick={handleSignOut} disabled={signingOut}>
          {signingOut ? 'Signing out…' : 'Sign out'}
        </button>}
        {freshness}
      </div>
      {verified && !preview && <p className="profile-sign-out-error" role="alert">
        {signOutFailed ? 'Couldn’t sign out. Try again.' : ''}
      </p>}
      <dialog ref={shortcuts} className="shortcuts-dialog" aria-labelledby={shortcutsTitle}
        onClose={restoreShortcutFocus}>
        <h2 id={shortcutsTitle}>Keyboard shortcuts</h2>
        <dl>
          <div><dt>Search tasks</dt><dd><kbd>Alt</kbd> + <kbd>Shift</kbd> + <kbd>S</kbd></dd></div>
          <div><dt>Show shortcuts</dt><dd><kbd>Alt</kbd> + <kbd>Shift</kbd> + <kbd>H</kbd></dd></div>
        </dl>
        <button type="button" autoFocus onClick={() => shortcuts.current.close()}>Close</button>
      </dialog>
    </div>
  )
}
