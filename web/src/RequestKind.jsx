import React from 'react'
import { CircleHelp, ClipboardCheck, Info, MessageCircle } from 'lucide-react'

const kinds = {
  fyi: [Info, 'FYI'],
  ask: [MessageCircle, 'Ask'],
  request: [MessageCircle, 'Request'],
  decision: [CircleHelp, 'Decision'],
  review: [ClipboardCheck, 'Review'],
}

export default function RequestKind({ kind }) {
  const [Icon, label] = kinds[kind] || kinds.request
  return <span className="request-kind"><Icon size={14} aria-hidden="true" />{label}</span>
}
