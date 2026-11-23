import beacon from '../../../docs/sponsor/avatars/beacon.svg'
import gauge from '../../../docs/sponsor/avatars/gauge.svg'
import herald from '../../../docs/sponsor/avatars/herald.svg'
import loom from '../../../docs/sponsor/avatars/loom.svg'
import prism from '../../../docs/sponsor/avatars/prism.svg'

import snapshot from './snapshot-data.json'

export const repository = 'https://github.com/skyzh/taskboard'
const profileSource = repository + '/blob/main/SPONSOR.md#the-taskboard-squad'

const avatars = { beacon, gauge, herald, loom, prism }
export const { capturedAt, queuedAsks } = snapshot
export const entries = snapshot.entries.map(entry => ({
  ...entry,
  public_url: profileSource,
  profile: { ...entry.profile, avatar_url: avatars[entry.owner] },
}))
export const profiles = new Map(entries.map(({ owner, profile }) => [owner, profile]))
