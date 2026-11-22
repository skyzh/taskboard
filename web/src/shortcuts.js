export function matchesBoardShortcut(event, code) {
  return event.code === code && event.altKey && event.shiftKey && !event.ctrlKey && !event.metaKey &&
    !event.defaultPrevented && !event.isComposing && !event.repeat &&
    !event.target?.closest?.('input, select, textarea, [contenteditable], [role="textbox"], dialog[open]')
}
