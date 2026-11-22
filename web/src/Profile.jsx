import React, { useEffect, useId, useRef, useState } from 'react'
import { request, signOut } from './data.js'
import { AssigneeAvatar, ResourceStatus, signInHref } from './shared.jsx'

export function ProfileForm({ actor, onSaved }) {
  const [role, setRole] = useState(actor.role || '')
  const [description, setDescription] = useState(actor.description || '')
  const [avatar, setAvatar] = useState(null)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState(false)
  const updates = {}
  if (role.trim() !== (actor.role || '')) updates.role = role.trim()
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

export default function Profile({ session, fragment, theme, freshness }) {
  const actor = session.data?.actor
  const panelId = useId()
  const profile = useRef(null)
  const trigger = useRef(null)
  const [open, setOpen] = useState(false)
  const [signingOut, setSigningOut] = useState(false)
  const [signOutFailed, setSignOutFailed] = useState(false)
  const verified = actor?.kind === 'human' || actor?.kind === 'agent'
  const name = verified ? actor.name : 'Not signed in'
  const nextMode = theme.mode === 'night' ? 'day' : 'night'

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
        <button type="button" onClick={() => { theme.chooseTheme(nextMode); closeAndFocus() }}>
          {nextMode === 'night' ? 'Night mode' : 'Day mode'}
        </button>
        <a href="#settings" onClick={closeAndFocus}>Settings</a>
        <a href="#about" onClick={closeAndFocus}>About</a>
        <ProfileSignIn session={session} fragment={fragment} />
        {verified && <button type="button" onClick={handleSignOut} disabled={signingOut}>
          {signingOut ? 'Signing out…' : 'Sign out'}
        </button>}
        {freshness}
        {signOutFailed && <p role="alert">Couldn’t sign out. Try again.</p>}
      </div>
    </div>
  )
}
