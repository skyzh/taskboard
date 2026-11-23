import React from 'react'
import './segmented.css'

export default function SegmentedControl({ label, value, options, onChange, className = '' }) {
  return (
    <div className={`segmented-control ${className}`.trim()} role="group" aria-label={label}>
      {options.map(option => (
        <button type="button" key={option.value} aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}>{option.label}</button>
      ))}
    </div>
  )
}
