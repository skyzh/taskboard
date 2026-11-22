import React from 'react'
import { DemoControl } from './DemoControl.jsx'
import './segmented.css'

export default function SegmentedControl({ label, value, options, onChange, className = '' }) {
  return (
    <div className={`segmented-control ${className}`.trim()} role="group" aria-label={label}>
      {options.map(option => (
        <DemoControl as="button" type="button" key={option.value} aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}>{option.label}</DemoControl>
      ))}
    </div>
  )
}
