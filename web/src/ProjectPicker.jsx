import React from 'react'
import { ChevronDown } from 'lucide-react'

export default function ProjectPicker({ projects, value, allProjects, onChange }) {
  const selected = (allProjects && value === 'all') || projects.some(project => project.prefix === value)
  return (
    <label className="project-switcher embedded-picker">
      <select aria-label="Project" value={selected ? value : ''} onChange={event => onChange(event.target.value)}>
        {!selected && <option value="" disabled>Choose project</option>}
        {allProjects && <option value="all">All projects</option>}
        {projects.map(project => <option key={project.prefix} value={project.prefix}>{project.title}</option>)}
      </select>
      <ChevronDown size={14} aria-hidden="true" focusable="false" />
    </label>
  )
}
