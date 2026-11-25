import React from 'react'
import { Pencil, PencilLine } from 'lucide-react'

export default function EditToggle({ label, active, onToggle }) {
  return (
    <button type="button" className="record-edit-toggle" aria-label={label} aria-pressed={active} onClick={onToggle}>
      {active ? <PencilLine size={18} aria-hidden="true" /> : <Pencil size={18} aria-hidden="true" />}
    </button>
  )
}
