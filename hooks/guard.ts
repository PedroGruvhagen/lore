// Pure MEMORY.md guard logic. No `$` here: the hooks module reads the file and calls these.

export type Limits = { maxLines: number; maxLineChars: number; maxBytes: number }

export type Measure = {
  lines: number
  bytes: number
  longCount: number
  longChars: number
  texts: string[]
}

export type Verdict = { deny: string } | { ok: true }

const POINTER = /lore:|global:|mem:|\]\([^)]*\)|->|=>|\.md\b|\/lore\//
const BULLET = /^\s*[-*+]\s/

export function normalizePath(p: string): string {
  const out: string[] = []
  for (const seg of p.replace(/\\/g, '/').split('/')) {
    if (seg === '..') out.pop()
    else if (seg !== '.' && seg !== '') out.push(seg)
  }
  return (p.startsWith('/') ? '/' : '') + out.join('/')
}

export function isIndexPath(p: string): boolean {
  return /(^|\/)memory\/MEMORY\.md$/.test(normalizePath(p))
}

export function splitLines(text: string): string[] {
  if (text === '') return []
  const parts = text.split('\n')
  if (parts[parts.length - 1] === '') parts.pop()
  return parts
}

export function measure(text: string, limits: Limits): Measure {
  const texts = splitLines(text)
  let longCount = 0
  let longChars = 0
  for (const line of texts) {
    if (line.length > limits.maxLineChars) {
      longCount += 1
      longChars += line.length - limits.maxLineChars
    }
  }
  return { lines: texts.length, bytes: new TextEncoder().encode(text).length, longCount, longChars, texts }
}

type EditArgs = {
  tool: string
  content?: unknown
  old_string?: unknown
  new_string?: unknown
  replace_all?: unknown
  edits?: unknown
}

function replaceOnce(text: string, from: string, to: string, all: boolean): string | null {
  if (from === '' || !text.includes(from)) return null
  return all ? text.split(from).join(to) : text.replace(from, () => to)
}

// The file text after the call, or null when it cannot be derived (the tool will fail or is unknown).
export function applyEdit(current: string | null, e: EditArgs): string | null {
  if (e.tool === 'Write') return typeof e.content === 'string' ? e.content : null
  if (current === null) return null
  if (e.tool === 'Edit') {
    if (typeof e.old_string !== 'string' || typeof e.new_string !== 'string') return null
    return replaceOnce(current, e.old_string, e.new_string, e.replace_all === true)
  }
  if (e.tool === 'MultiEdit' && Array.isArray(e.edits)) {
    let text = current
    for (const step of e.edits) {
      if (typeof step !== 'object' || step === null) return null
      const s = step as Record<string, unknown>
      if (typeof s.old_string !== 'string' || typeof s.new_string !== 'string') return null
      const next = replaceOnce(text, s.old_string, s.new_string, s.replace_all === true)
      if (next === null) return null
      text = next
    }
    return text
  }
  return null
}

// Deny only what makes the index worse than it is now, so a shrinking rewrite of an
// already-oversize file passes. A new file must be compliant outright. Reasons carry
// numbers only, never file content.
export function check(current: string | null, proposed: string, limits: Limits): Verdict {
  const before = measure(current ?? '', limits)
  const after = measure(proposed, limits)
  const reasons: string[] = []

  const lineExcess = (m: Measure) => Math.max(0, m.lines - limits.maxLines)
  const byteExcess = (m: Measure) => Math.max(0, m.bytes - limits.maxBytes)
  if (lineExcess(after) > lineExcess(before)) {
    reasons.push(`it grows the index to ${after.lines} lines (limit ${limits.maxLines})`)
  }
  if (byteExcess(after) > byteExcess(before)) {
    reasons.push(`it grows the index to ${after.bytes} bytes (limit ${limits.maxBytes})`)
  }
  if (after.longCount > before.longCount || after.longChars > before.longChars) {
    reasons.push(
      `${after.longCount} line(s) exceed ${limits.maxLineChars} characters and this change adds length to them or adds more`,
    )
  }

  const known = new Map<string, number>()
  for (const line of before.texts) known.set(line, (known.get(line) ?? 0) + 1)
  let bare = 0
  for (const line of after.texts) {
    const seen = known.get(line) ?? 0
    if (seen > 0) {
      known.set(line, seen - 1)
      continue
    }
    if (BULLET.test(line) && !POINTER.test(line)) bare += 1
  }
  if (bare > 0) reasons.push(`${bare} new list line(s) have no pointer to a Lore page`)

  if (reasons.length === 0) return { ok: true }
  return { deny: denyMessage(reasons, limits) }
}

export function denyMessage(reasons: string[], limits: Limits): string {
  return [
    'MEMORY.md is an index, not a store, and this write was refused: ' + reasons.join('; ') + '.',
    `Limits: ${limits.maxLines} lines, ${limits.maxLineChars} characters per line, ${limits.maxBytes} bytes; one line per fact in the form "- <short hook> -> lore:<page-slug>".`,
    'Put the detail in the matching Lore page instead: find it with mcp__lore__recall, edit it in place (or create it if none exists), then add only one short pointer line here. Edits that shorten or fix existing lines are allowed.',
  ].join(' ')
}

const WRITE_PATTERNS: RegExp[] = [
  />>?\s*['"]?\S*memory\/MEMORY\.md/,
  /\btee\b[^|;&]*memory\/MEMORY\.md/,
  /\b(sed|perl)\b[^|;&]*\s-[a-zA-Z]*i[^|;&]*memory\/MEMORY\.md/,
  /\b(mv|cp|install|ln)\b[^|;&]*memory\/MEMORY\.md['"]?\s*($|[;&|)])/,
  /\b(truncate|dd)\b[^|;&]*memory\/MEMORY\.md/,
]

// Heuristic: a shell command that visibly writes a MEMORY.md. Cannot see through variables,
// scripts, subshell expansion or other interpreters; the nightly gardener check is the backstop.
export function bashWritesIndex(command: string): boolean {
  return WRITE_PATTERNS.some((re) => re.test(command))
}

export function bashDenyMessage(): string {
  return 'A shell write to MEMORY.md was refused because the size check cannot run on it. Use the Edit or Write tool on that file so the index limits can be checked, and keep MEMORY.md to one short pointer line per fact; detail belongs in the Lore page (mcp__lore__recall to find it).'
}
