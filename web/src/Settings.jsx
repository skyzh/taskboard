import React, { useId } from 'react'
import CodexUsage from './CodexUsage.jsx'
import FieldGuidanceSettings from './FieldGuidanceSettings.jsx'
import { ProfileForm, ProfileSignIn } from './Profile.jsx'
import { ResourceStatus } from './shared.jsx'
import { browserTimeZone, timeZones } from './timeZone.js'

const themeChoices = [
  { value: 'day', label: 'Day' },
  { value: 'night', label: 'Night' },
  { value: '', label: 'Auto' },
]

export default function Settings({ session, theme, timeZonePreference, chooseTimeZone, onProfileSaved, project, projects, revision }) {
  const browserZoneId = useId()
  const usesBrowserZone = !timeZonePreference
  const actor = session.data?.actor
  const canEdit = actor?.kind === 'agent' || actor?.kind === 'human'
  return (
    <section className="settings" aria-labelledby="settings">
      <div className="page-heading"><h1 id="settings">Settings</h1></div>
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
      </section>
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
      {canEdit && <FieldGuidanceSettings project={project} projects={projects} revision={revision} />}
      {actor?.kind === 'human' && <CodexUsage key={actor.id} />}
    </section>
  )
}
