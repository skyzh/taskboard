import React, { useEffect, useMemo, useRef, useState } from 'react'
import { queryPath, request } from './data.js'
import { ResourceStatus, Status, profileName, taskLabel, taskState } from './shared.jsx'
import { MarkdownInline } from './Markdown.jsx'
import './milestone-map.css'

export async function loadMilestones(signal, project) {
  const items = []
  let before
  do {
    const page = await request(queryPath('/milestones', { limit: 20, before, project }), { signal })
    items.push(...page.items)
    before = page.next_cursor
  } while (before != null)
  return items
}

function useMilestones(revision, project) {
  const [attempt, setAttempt] = useState(0)
  const [result, setResult] = useState({})

  useEffect(() => {
    const controller = new AbortController()
    loadMilestones(controller.signal, project).then(
      items => {
        if (!controller.signal.aborted) setResult({ revision, project, attempt, items })
      },
      error => {
        if (!controller.signal.aborted) setResult(previous => ({
          revision, project, attempt, error, items: previous.project === project ? previous.items : undefined,
        }))
      },
    )
    return () => controller.abort()
  }, [revision, project, attempt])

  const current = result.revision === revision && result.project === project && result.attempt === attempt
  return {
    items: result.project === project ? result.items : undefined,
    pending: !current && (!result.items || result.project !== project),
    error: current ? result.error : undefined,
    retry: () => setAttempt(value => value + 1),
  }
}

const detourSpacing = 8

export function buildMilestoneGraph(items) {
  const nodes = new Map(items.map(({ task, workstream }) => [
    task.id, { task, workstream, incoming: [], outgoing: [] },
  ]))
  const edges = []
  for (const { task, prerequisites } of items) {
    const target = nodes.get(task.id)
    for (const prerequisite of prerequisites) {
      if (!nodes.has(prerequisite.id)) {
        nodes.set(prerequisite.id, { task: prerequisite, workstream: null, incoming: [], outgoing: [] })
      }
      const source = nodes.get(prerequisite.id)
      const edge = { source, target, key: `${source.task.id}-${target.task.id}` }
      source.outgoing.push(edge)
      target.incoming.push(edge)
      edges.push(edge)
    }
  }

  const ordered = [...nodes.values()].sort((a, b) =>
    (a.workstream || '').localeCompare(b.workstream || '', 'en', { sensitivity: 'base' }) || a.task.id - b.task.id,
  )
  const remaining = new Map(ordered.map(node => [node, node.incoming.length]))
  const ready = ordered.filter(node => !node.incoming.length)
  let lastColumn = -1
  for (const node of ready) {
    node.column = node.incoming.reduce((column, { source }) => Math.max(column, source.column + 1), 0)
    lastColumn = Math.max(lastColumn, node.column)
    for (const { target } of node.outgoing) {
      const count = remaining.get(target) - 1
      remaining.set(target, count)
      if (!count) ready.push(target)
    }
  }

  const columns = []
  for (const node of ordered) {
    // The ledger allows cycles, so keep their nodes and arrows in the diagram.
    node.column ??= lastColumn + 1
    columns[node.column] ??= []
    columns[node.column].push(node)
  }

  const gutters = columns.map(() => [])
  let detours = 0
  for (const edge of edges) {
    if (edge.target.column === edge.source.column + 1) {
      gutters[edge.source.column].push(edge)
    } else {
      edge.track = detours++
    }
  }
  for (const gutter of gutters) {
    gutter.forEach((edge, index) => {
      edge.gutterPosition = .3 + .4 * (index + 1) / (gutter.length + 1)
    })
  }
  return { columns, edges, detourHeight: detours * detourSpacing }
}

export function drawConnections(canvas, diagram, edges) {
  if (!canvas.getClientRects().length) return
  const context = canvas.getContext('2d')
  if (!context) return

  const frame = diagram.getBoundingClientRect()
  const view = canvas.ownerDocument.defaultView
  const scale = view.devicePixelRatio || 1
  canvas.width = Math.ceil(frame.width * scale)
  canvas.height = Math.ceil(frame.height * scale)
  context.scale(scale, scale)
  const color = view.getComputedStyle(canvas).color
  context.strokeStyle = color
  context.fillStyle = color
  context.lineWidth = 1.5
  context.lineJoin = 'round'

  const cards = new Map([...diagram.querySelectorAll('[data-milestone-id]')].map(card => [
    card.dataset.milestoneId, card.getBoundingClientRect(),
  ]))
  const rows = new Map([...diagram.querySelectorAll('[data-milestone-edge]')].map(row => [
    row.dataset.milestoneEdge, row.getBoundingClientRect(),
  ]))

  for (const { source, target, key, gutterPosition, track } of edges) {
    const from = cards.get(String(source.task.id))
    const to = cards.get(String(target.task.id))
    const label = rows.get(key)
    const start = { x: from.right - frame.left, y: from.top - frame.top + 20 }
    const end = { x: to.left - frame.left - 2, y: label.top - frame.top + label.height / 2 }

    context.beginPath()
    context.moveTo(start.x, start.y)
    if (track == null) {
      const turn = start.x + (end.x - start.x) * gutterPosition
      context.lineTo(turn, start.y)
      context.lineTo(turn, end.y)
    } else {
      const top = 10 + track * detourSpacing
      context.lineTo(start.x + 10, start.y)
      context.lineTo(start.x + 10, top)
      context.lineTo(end.x - 12, top)
      context.lineTo(end.x - 12, end.y)
    }
    context.lineTo(end.x - 5, end.y)
    context.stroke()

    context.beginPath()
    context.moveTo(end.x, end.y)
    context.lineTo(end.x - 6, end.y - 3.5)
    context.lineTo(end.x - 6, end.y + 3.5)
    context.closePath()
    context.fill()
  }
}

function MilestoneCard({ node, taskHref }) {
  const { task, workstream, incoming } = node
  return (
    <li className="milestone-map-card" data-milestone-id={task.id}>
      <p className="milestone-map-workstream">{workstream || 'Prerequisite task'}</p>
      <h3 className="milestone-map-task">
        <a href={taskHref(task.id, task.key)}>
          <span className="milestone-map-id">{taskLabel(task.id, task.key)}</span> <MarkdownInline text={task.title} links={false} />
        </a>
      </h3>
      <div className="milestone-map-meta">
        <Status state={taskState(task)} />
        {task.dri && <span>Owner {profileName(task.dri, task.dri_profile)}</span>}
      </div>
      {incoming.length > 0 && (
        <div className="milestone-map-prerequisites">
          <p>Prerequisites</p>
          <ul role="list" aria-label={`Prerequisites for ${taskLabel(task.id, task.key)}`}>
            {incoming.map(({ source, key }) => (
              <li key={key} data-milestone-edge={key}>
                <span className="milestone-map-narrow-arrow" aria-hidden="true">↑</span>
                <div>
                  <a href={taskHref(source.task.id, source.task.key)}>
                    <span className="milestone-map-id">{taskLabel(source.task.id, source.task.key)}</span> <MarkdownInline text={source.task.title} links={false} />
                  </a>
                  {source.workstream && source.workstream !== workstream && (
                    <span className="milestone-map-source-workstream">{source.workstream}</span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </li>
  )
}

export function MilestoneDiagram({ items, taskHref }) {
  const graph = useMemo(() => buildMilestoneGraph(items), [items])
  const [overflows, setOverflows] = useState(false)
  const viewportRef = useRef(null)
  const diagramRef = useRef(null)
  const canvasRef = useRef(null)

  useEffect(() => {
    const viewport = viewportRef.current
    const diagram = diagramRef.current
    const canvas = canvasRef.current
    let frame
    const resize = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        setOverflows(viewport.scrollWidth > viewport.clientWidth)
        drawConnections(canvas, diagram, graph.edges)
      })
    }
    const observer = new ResizeObserver(resize)
    observer.observe(viewport)
    observer.observe(diagram)
    for (const element of diagram.querySelectorAll('[data-milestone-id], [data-milestone-edge]')) {
      observer.observe(element)
    }
    resize()
    return () => {
      observer.disconnect()
      cancelAnimationFrame(frame)
    }
  }, [graph])

  return (
    <>
      <p className="milestone-map-explanation">Arrows run from each prerequisite to the milestone that depends on it.</p>
      <div
        className="milestone-map-viewport"
        ref={viewportRef}
        role="region"
        aria-label="Milestone dependency diagram"
        tabIndex={overflows ? 0 : undefined}
      >
        <div
          className="milestone-map-diagram"
          ref={diagramRef}
          style={{ '--milestone-columns': graph.columns.length, '--milestone-detour-height': `${graph.detourHeight}px` }}
        >
          <canvas className="milestone-map-connections" ref={canvasRef} aria-hidden="true" />
          {graph.columns.map((column, index) => (
            <ul className="milestone-map-column" role="list" key={index}>
              {column.map(node => <MilestoneCard key={node.task.id} node={node} taskHref={taskHref} />)}
            </ul>
          ))}
        </div>
      </div>
    </>
  )
}

export default function MilestoneMap({ taskHref, revision, project }) {
  const resource = useMilestones(revision, project)
  return (
    <div className="milestone-map">
      <ResourceStatus resource={resource} label="workstreams" />
      {resource.items?.length === 0 && <p className="section-message">No milestones yet.</p>}
      {resource.items?.length > 0 && <MilestoneDiagram items={resource.items} taskHref={taskHref} />}
    </div>
  )
}
