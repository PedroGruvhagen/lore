import type { Register } from 'claude-code'
import { applyEdit, bashDenyMessage, bashWritesIndex, check, isIndexPath, measure } from './guard'
import { RULES, indexPath, trimIndex } from './digest'

// Memory content (prompts, tool arguments, file contents, remembered text) is never logged.
// Log lines carry names, counts and decisions only.

export const register: Register = (on, options) => {
  const limits = {
    maxLines: Number(options.maxLines),
    maxLineChars: Number(options.maxLineChars),
    maxBytes: Number(options.maxBytes),
  }
  const loadIndex = options.loadIndex !== false
  // The path options have no defaults. Unset, the feature that needs them stays off (fail open).
  const str = (v: unknown): string => (typeof v === 'string' ? v : '')
  const projectsDir = str(options.projectsDir)
  const python = str(options.python)
  const lorePy = str(options.lorePy)
  const loreDir = str(options.loreDir)
  const cliReady = python !== '' && lorePy !== '' && loreDir !== ''
  const NOT_CONFIGURED = 'lore is not configured: set python, lorePy and loreDir in the plugin options'
  const accountDirs = Array.isArray(options.accountDirs) ? options.accountDirs.map(String) : []

  on('session.start', async ($, e, next) => {
    try {
      await $.tool.register({
        name: 'recall',
        description: 'Search Lore, the long-term knowledge base, for a project, person, server or technical fact. Returns ranked page snippets; read the top page before acting.',
        inputSchema: {
          type: 'object',
          properties: { query: { type: 'string', description: 'What to look up' } },
          required: ['query'],
        },
      })
      await $.tool.register({
        name: 'remember',
        description: 'Drop a durable fact into the Lore inbox; the nightly gardener files it into the right page. Use for lasting facts, never session chatter.',
        inputSchema: {
          type: 'object',
          properties: { text: { type: 'string', description: 'The fact to keep' } },
          required: ['text'],
        },
      })
      await $.command.register({ name: 'lore', description: 'Lore status: index size, over-cap lines, gardener, auto-memory', immediate: true })
    } catch {
      $.ui.log('lore: registration failed', { to: 'debug' })
    }
    return next(e)
  })

  // The MEMORY.md guard. No `.catch` on purpose: if this hook fails or times out it is
  // skipped and the write proceeds (fail open); the gardener check is the backstop.
  on('tool.call', { tool: ['Write', 'Edit', 'MultiEdit'] }, async ($, e, next) => {
    try {
      const path = typeof e.file_path === 'string' ? e.file_path : ''
      if (!isIndexPath(path)) return next(e)
      let current: string | null = null
      try {
        current = await $.fs.read(path)
      } catch {
        current = null
      }
      const proposed = applyEdit(current, e)
      if (proposed === null) return next(e)
      const verdict = check(current, proposed, limits)
      if ('deny' in verdict) {
        const m = measure(proposed, limits)
        $.ui.log('lore: guard denied ' + e.tool + ' on MEMORY.md, lines=' + m.lines + ' bytes=' + m.bytes, { to: 'debug' })
        return { deny: verdict.deny }
      }
      $.ui.log('lore: guard allowed ' + e.tool + ' on MEMORY.md', { to: 'debug' })
    } catch {
      $.ui.log('lore: guard error, failing open', { to: 'debug' })
    }
    return next(e)
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    try {
      if (typeof e.command === 'string' && bashWritesIndex(e.command)) {
        $.ui.log('lore: guard denied a shell write to MEMORY.md', { to: 'debug' })
        return { deny: bashDenyMessage() }
      }
    } catch {
      $.ui.log('lore: bash guard error, failing open', { to: 'debug' })
    }
    return next(e)
  })

  on('tool.call', { tool: 'mcp__lore__recall' }, async ($, e) => {
    try {
      const query = typeof e.query === 'string' ? e.query : ''
      if (query === '') return { result: 'lore recall needs a query' }
      if (!cliReady) return { result: NOT_CONFIGURED }
      const r = await $.process.run([python, lorePy, 'search', '--lore-dir', loreDir, '--limit', '5', '--', query])
      if (r.exitCode !== 0) {
        $.ui.log('lore: recall failed, exit ' + r.exitCode, { to: 'debug' })
        return { result: 'lore recall failed (exit ' + r.exitCode + ')' }
      }
      return { result: r.stdout.slice(0, 8000) }
    } catch {
      $.ui.log('lore: recall error', { to: 'debug' })
      return { result: 'lore recall failed' }
    }
  })

  // remember passes the text to lore.py inbox exactly as given: no filtering, no warning.
  on('tool.call', { tool: 'mcp__lore__remember' }, async ($, e) => {
    try {
      const text = typeof e.text === 'string' ? e.text : ''
      if (text === '') return { result: 'lore remember needs text' }
      if (!cliReady) return { result: NOT_CONFIGURED }
      const r = await $.process.run([python, lorePy, 'inbox', '--lore-dir', loreDir, '--', text])
      if (r.exitCode !== 0) {
        $.ui.log('lore: remember failed, exit ' + r.exitCode, { to: 'debug' })
        return { result: 'lore remember failed (exit ' + r.exitCode + ')' }
      }
      $.ui.log('lore: remember stored a note', { to: 'debug' })
      return { result: 'Stored in the Lore inbox; the nightly gardener files it into the right page.' }
    } catch {
      $.ui.log('lore: remember error', { to: 'debug' })
      return { result: 'lore remember failed' }
    }
  })

  // The loader of the MEMORY.md index. The harness loads the index as an instruction file of kind
  // `memory` inside the first message's `claudeMd` context block (so subagents get it too); this hook
  // adds the same kind of file, as the built-in agents-md mod does for AGENTS.md. When core already
  // supplies a memory file (auto-memory on) the mod adds nothing, so the index is never loaded twice.
  on('prompt.context', async ($, e, next) => {
    const core = await next(e)
    if (!loadIndex || projectsDir === '') return core
    try {
      const files = core.instructionFiles
      if (files !== undefined && files.some((f) => f.kind === 'memory')) return core
      const here = await $.session.cwd()
      // Claude Code names the project folder after the git repo's canonical root, so a
      // subdirectory or a worktree shares the main checkout's memory; outside git it is the cwd.
      const dirs: string[] = []
      try {
        const g = await $.process.run(['/usr/bin/git', '-C', here, 'rev-parse', '--path-format=absolute', '--git-common-dir'])
        const common = g.exitCode === 0 ? g.stdout.trim() : ''
        if (common.endsWith('/.git')) dirs.push(common.slice(0, -5))
      } catch {
        $.ui.log('lore: git root lookup failed', { to: 'debug' })
      }
      for (const d of [here, await $.session.root()]) {
        if (typeof d === 'string' && d !== '' && !dirs.includes(d)) dirs.push(d)
      }
      let path: string | null = null
      for (const d of dirs) {
        const p = indexPath(projectsDir, d)
        if (await $.fs.exists(p)) {
          path = p
          break
        }
      }
      const text = path === null ? null : trimIndex(await $.fs.read(path), limits.maxBytes)
      $.ui.log('lore: context load index=' + (text === null ? 'none' : 'found') + ' as=' + (files === undefined ? 'block' : 'file'), { to: 'debug' })
      const blocks = [...core.blocks, { name: 'loreRules', text: RULES }]
      if (text === null || text.trim() === '') return { blocks, instructionFiles: files }
      if (files === undefined) return { blocks: [...blocks, { name: 'loreIndex', text }] }
      return { blocks, instructionFiles: [...files, { path: path as string, kind: 'memory' as const, content: text }] }
    } catch {
      $.ui.log('lore: context load error, failing open', { to: 'debug' })
      return core
    }
  })

  // Never blocks compaction.
  on('session.compact', async ($, e, next) => {
    if (e.trigger === 'manual' || e.trigger === 'auto') {
      try {
        $.ui.toast('lore: compacting. Durable facts belong in Lore (mcp__lore__remember).')
      } catch {
        $.ui.log('lore: compact notice failed', { to: 'debug' })
      }
    }
    return next(e)
  })

  on('command.run', { command: 'lore' }, async ($) => {
    const out: string[] = ['lore mod 0.1.0: active']
    try {
      const dirs: string[] = []
      const here = await $.session.cwd()
      try {
        const g = await $.process.run(['/usr/bin/git', '-C', here, 'rev-parse', '--path-format=absolute', '--git-common-dir'])
        const common = g.exitCode === 0 ? g.stdout.trim() : ''
        if (common.endsWith('/.git')) dirs.push(common.slice(0, -5))
      } catch {
        $.ui.log('lore: git root lookup failed', { to: 'debug' })
      }
      for (const d of [await $.session.root(), here]) {
        if (typeof d === 'string' && d !== '' && !dirs.includes(d)) dirs.push(d)
      }
      let found = false
      for (const d of dirs) {
        if (projectsDir === '') break
        const p = indexPath(projectsDir, d)
        if (!(await $.fs.exists(p))) continue
        const m = measure(await $.fs.read(p), limits)
        const over: number[] = []
        m.texts.forEach((line, i) => {
          if (line.length > limits.maxLineChars) over.push(i + 1)
        })
        out.push('index: ' + m.lines + ' lines (limit ' + limits.maxLines + '), ' + m.bytes + ' bytes (limit ' + limits.maxBytes + ')')
        out.push('over-cap lines: ' + over.length + (over.length > 0 ? ' at ' + over.slice(0, 10).join(', ') + (over.length > 10 ? ', ...' : '') : ''))
        found = true
        break
      }
      if (!found) out.push('index: none for this project')
    } catch {
      out.push('index: could not be read')
    }
    try {
      if (loreDir === '') throw new Error('loreDir unset')
      const entries = await $.fs.list(loreDir + '/.gardener')
      const reports = entries.map((x) => x.name).filter((n) => /^report-\d{4}-\d{2}-\d{2}\.md$/.test(n)).sort()
      out.push('last gardener report: ' + (reports.length > 0 ? reports[reports.length - 1] : 'none'))
    } catch {
      out.push('last gardener report: unreadable')
    }
    out.push('index loading by this mod: ' + (loadIndex ? 'on' : 'off'))
    for (const dir of accountDirs) {
      try {
        const r = await $.process.run(['/usr/bin/grep', '-Eq', '"autoMemoryEnabled"[[:space:]]*:[[:space:]]*false', dir + '/settings.json'])
        const state = r.exitCode === 0 ? 'OFF' : r.exitCode === 1 ? 'ON (not disabled)' : 'settings.json unreadable'
        out.push('auto-memory ' + dir.split('/').pop() + ': ' + state)
      } catch {
        out.push('auto-memory ' + dir.split('/').pop() + ': check failed')
      }
    }
    return { text: out.join('\n') }
  })
}
