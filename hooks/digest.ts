// Pure session-load helpers. Output is a function of the MEMORY.md text alone, with no
// timestamps, so the injected block is byte-stable between requests (prompt cache).

export const RULES = [
  'Lore memory rules:',
  '- MEMORY.md is an index only: one line per fact, a short hook plus a pointer to a Lore page. Detail lives in the page.',
  '- Before touching a subsystem or answering about a named project, person, server or technical fact, search Lore (mcp__lore__recall) and read the page first.',
  '- Save only durable facts, never session chatter. Edit the matching Lore page in place; for a quick capture use mcp__lore__remember.',
].join('\n')

// Claude Code names a project folder by replacing every non-alphanumeric character with "-".
export function projectDirName(path: string): string {
  return path.replace(/[^A-Za-z0-9]/g, '-')
}

export function indexPath(projectsDir: string, workdir: string): string {
  return projectsDir.replace(/\/+$/, '') + '/' + projectDirName(workdir) + '/memory/MEMORY.md'
}

// The index text capped at maxBytes on a line boundary; a pure function of the file, so stable.
export function trimIndex(indexText: string, maxBytes: number): string {
  let body = indexText.replace(/\s+$/, '')
  const enc = new TextEncoder()
  if (enc.encode(body).length > maxBytes) {
    const lines = body.split('\n')
    const kept: string[] = []
    let used = 0
    for (const line of lines) {
      used += enc.encode(line + '\n').length
      if (used > maxBytes) break
      kept.push(line)
    }
    body = kept.join('\n') + '\n[index truncated at ' + maxBytes + ' bytes]'
  }
  return body
}
