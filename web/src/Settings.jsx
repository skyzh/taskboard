import React, { useId } from 'react'
import CodexUsage from './CodexUsage.jsx'
import FieldGuidanceSettings from './FieldGuidanceSettings.jsx'
import { ProfileForm, ProfileSignIn } from './Profile.jsx'
import ProjectSettings from './ProjectSettings.jsx'
import ProjectOwnerSettings from './ProjectOwnerSettings.jsx'
import { ResourceStatus } from './shared.jsx'
import { browserTimeZone, timeZones } from './timeZone.js'

const themeChoices = [
  { value: 'day', label: 'Day' },
  { value: 'night', label: 'Night' },
  { value: '', label: 'Auto' },
]

export function AppearanceSettings({ theme, timeZonePreference, chooseTimeZone, children }) {
  const browserZoneId = useId()
  const usesBrowserZone = !timeZonePreference
  return (
    <section className="panel settings-panel" aria-labelledby="appearance-heading">
      <h2 id="appearance-heading">Appearance</h2>
      <fieldset className="settings-preference theme-preference">
        <legend>Color theme</legend>
        <div className="theme-choices">
          {themeChoices.map(({ value, label }) => (
            <label key={value}>
              <input type="radio" name="color-theme" value={value} checked={theme.preference === value}
                onChange={event => theme.chooseTheme(event.currentTarget.value)} />
              {label}
            </label>
          ))}
        </div>
      </fieldset>
      <label className="settings-preference">
        Display time zone
        <select value={timeZonePreference ?? ''} onChange={event => chooseTimeZone(event.target.value)}
          aria-describedby={usesBrowserZone ? browserZoneId : undefined}>
          <option value="">Browser time zone</option>
          {timeZones.map(zone => <option key={zone} value={zone}>{zone}</option>)}
        </select>
      </label>
      {usesBrowserZone && <p className="settings-browser-zone" id={browserZoneId}>{browserTimeZone}</p>}
      {children}
    </section>
  )
}

export default function Settings({
  session, theme, timeZonePreference, chooseTimeZone, showWelcomeBanner, chooseWelcomeBanner,
  onProfileSaved, project, projects, revision, onProjectCreated,
}) {
  const actor = session.data?.actor
  const canEdit = actor?.kind === 'agent' || actor?.kind === 'human'
  return (
    <section className="settings" aria-labelledby="settings">
      <div className="page-heading"><h1 id="settings">Settings</h1></div>
      <AppearanceSettings theme={theme} timeZonePreference={timeZonePreference} chooseTimeZone={chooseTimeZone}>
        <label className="settings-preference settings-welcome-banner">
          <input type="checkbox" checked={showWelcomeBanner}
            onChange={event => chooseWelcomeBanner(event.currentTarget.checked)} />
          Show welcome banner on Reports
        </label>
      </AppearanceSettings>
      {(canEdit || session.pending || session.error || session.data?.sign_in_available) && (
        <section className="panel settings-panel" aria-labelledby="profile-heading">
          <h2 id="profile-heading">Profile</h2>
          {canEdit ? <ProfileForm key={actor.id ?? actor.name} actor={actor} onSaved={onProfileSaved} /> : (
            <>
              {session.error?.status !== 401 && <ResourceStatus resource={session} label="your session" />}
              <ProfileSignIn session={session} fragment="#settings" />
            </>
          )}
        </section>
      )}
      {canEdit && <ProjectSettings key={actor.id ?? actor.name} revision={revision} onProjectCreated={onProjectCreated} />}
      {canEdit && <ProjectOwnerSettings project={project} projects={projects} revision={revision} />}
      {canEdit && <FieldGuidanceSettings project={project} projects={projects} revision={revision} />}
      {actor?.kind === 'human' && <CodexUsage key={actor.id} />}
    </section>
  )
}
