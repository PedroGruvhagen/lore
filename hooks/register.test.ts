import { expect, test as baseTest } from 'claude-code/testing'

// The plugin has no machine-specific defaults, so every test supplies neutral options.
const BASE_OPTIONS = {
  projectsDir: '/home/user/.claude/projects',
  python: '/usr/bin/python3',
  lorePy: '/home/user/.claude/skills/lore/scripts/lore.py',
  loreDir: '/home/user/.claude/lore',
  accountDirs: ['/home/user/.claude', '/home/user/.claude-b'],
}
const test: typeof baseTest = ((name: string, a: any, b?: any) =>
  b === undefined
    ? (baseTest as any)(name, { options: BASE_OPTIONS }, a)
    : (baseTest as any)(name, { ...a, options: { ...BASE_OPTIONS, ...a.options } }, b)) as any
import { RULES } from './digest'

const WORKDIR = '/home/user/Work/demo'
const INDEX_PATH = '/home/user/.claude/projects/-home-user-Work-demo/memory/MEMORY.md'
const INDEX_TEXT = '# Memory Index\n- Deploy gotchas -> lore:deploy-gotchas\n- Server map -> lore:server-map\n'
const SECRET = 'sk-test-SECRET-0123456789abcdefghijklmnop'
const MARKER = 'ZETA-MEMORY-CONTENT-MARKER-7731'

function stubs(on: any, opts: { index?: string | null; runs?: any[]; logs?: string[]; stdout?: string; exitCode?: number; registered?: string[]; gitCommon?: string; cwd?: string } = {}) {
  const index = opts.index === undefined ? INDEX_TEXT : opts.index
  const cwd = opts.cwd ?? WORKDIR
  on('session.root', () => ({ value: cwd }))
  on('session.cwd', () => ({ value: cwd }))
  on('fs.exists', (_$: any, e: any) => ({ value: index !== null && e.path === INDEX_PATH }))
  on('fs.read', (_$: any, e: any) => (index !== null && e.path === INDEX_PATH ? { value: index } : { deny: 'ENOENT' }))
  on('ui.log', (_$: any, e: any) => {
    opts.logs?.push(JSON.stringify(e))
    return { value: undefined }
  })
  on('ui.toast', () => ({ value: undefined }))
  on('tool.register', (_$: any, e: any) => {
    opts.registered?.push(e.name)
    return { value: { tool: 'mcp__lore__' + e.name } }
  })
  on('command.register', (_$: any, e: any) => {
    opts.registered?.push('/' + e.name)
    return { value: { command: e.name } }
  })
  on('prompt.context', (_$: any, e: any) => ({ blocks: e.blocks, instructionFiles: e.instructionFiles }))
  on('session.start', (_$: any, e: any) => ({ cwd: e.cwd }))
  on('process.run', (_$: any, e: any) => {
    if (e.argv[0] === '/usr/bin/git') {
      return opts.gitCommon
        ? { value: { exitCode: 0, stdout: opts.gitCommon + '\n', stderr: '' } }
        : { value: { exitCode: 128, stdout: '', stderr: 'not a git repository' } }
    }
    opts.runs?.push(e.argv)
    return { value: { exitCode: opts.exitCode ?? 0, stdout: opts.stdout ?? '', stderr: '' } }
  })
  on('tool.call', () => ({ result: 'bottom' }))
}

const ctxOf = (r: any) => {
  const file = (r.instructionFiles ?? []).find((f: any) => f.kind === 'memory')
  return { file, rules: (r.blocks ?? []).find((b: any) => b.name === 'loreRules') }
}
const open = (extra: any = {}) => ({ blocks: [{ name: 'claudeMd', text: 'x' }], instructionFiles: [], ...extra })

test('prompt.context adds the cwd index as a memory instruction file plus the rules block', async ($, on) => {
  stubs(on)
  const { file, rules } = ctxOf(await $.prompt.context(open()))
  expect(file.path).toBe(INDEX_PATH)
  expect(file.content).toContain('Server map -> lore:server-map')
  expect(rules.text).toBe(RULES)
})

test('a memory file already supplied by core (auto-memory on) is left alone: no double load', async ($, on) => {
  stubs(on)
  const core = { path: '/core/MEMORY.md', kind: 'memory' as const, content: 'from core' }
  const r = await $.prompt.context(open({ instructionFiles: [core] }))
  expect(r.instructionFiles).toEqual([core])
  expect(ctxOf(r).rules).toBeUndefined()
})

test('the injected block is byte-stable between conversations', async ($, on) => {
  stubs(on)
  const a = await $.prompt.context(open())
  const b = await $.prompt.context(open())
  expect(JSON.stringify(a)).toBe(JSON.stringify(b))
})

test('no index file: only the rules block is added, nothing breaks', async ($, on) => {
  stubs(on, { index: null })
  const r = await $.prompt.context(open())
  const { file, rules } = ctxOf(r)
  expect(file).toBeUndefined()
  expect(rules.text).toBe(RULES)
})

test('loadIndex=false adds nothing', { options: { loadIndex: false } }, async ($, on) => {
  stubs(on)
  const r = await $.prompt.context(open())
  expect(r.blocks.length).toBe(1)
  expect(r.instructionFiles).toEqual([])
})

test('unknown instruction files (claudeMd rewritten above): the index rides as its own block', async ($, on) => {
  stubs(on)
  const r = await $.prompt.context({ blocks: [{ name: 'claudeMd', text: 'x' }] })
  expect((r.blocks as any[]).some((b) => b.name === 'loreIndex' && b.text.includes('Server map'))).toBe(true)
})

test('session.start registers recall, remember and /lore', async ($, on) => {
  const registered: string[] = []
  stubs(on, { registered })
  await $.session.start({ cwd: WORKDIR, surface: null, isInteractive: false })
  expect(registered).toEqual(['recall', 'remember', '/lore'])
})

test('recall calls lore.py search with an argv array and returns its output', async ($, on) => {
  const runs: any[] = []
  stubs(on, { runs, stdout: '[1] Project registry (pages/project-registry.md)' })
  const r = await $.tool.call({ tool: 'mcp__lore__recall', query: 'project --help' })
  expect(r.result).toContain('Project registry')
  const argv = runs[0]
  expect(argv.slice(1, 3)).toEqual(['/home/user/.claude/skills/lore/scripts/lore.py', 'search'])
  expect(argv[argv.length - 1]).toBe('project --help')
  expect(argv[argv.length - 2]).toBe('--')
})

test('remember passes a secret-looking string to lore.py unchanged', async ($, on) => {
  const runs: any[] = []
  const logs: string[] = []
  stubs(on, { runs, logs })
  const text = `server key is ${SECRET}, I'll need it later`
  const r = await $.tool.call({ tool: 'mcp__lore__remember', text })
  expect(runs.length).toBe(1)
  expect(runs[0][2]).toBe('inbox')
  expect(runs[0][runs[0].length - 1]).toBe(text)
  expect(JSON.stringify(r)).not.toContain(SECRET)
  expect(logs.join('\n')).not.toContain(SECRET)
})

test('a failing lore.py never throws and never echoes the text', async ($, on) => {
  const logs: string[] = []
  stubs(on, { logs, exitCode: 2 })
  const r = await $.tool.call({ tool: 'mcp__lore__remember', text: SECRET })
  expect(String(r.result)).toContain('failed')
  expect(JSON.stringify(r)).not.toContain(SECRET)
  expect(logs.join('\n')).not.toContain(SECRET)
})

test('no log line from any hook contains memory content', async ($, on) => {
  const logs: string[] = []
  stubs(on, { logs, index: `- ${MARKER} -> lore:x\n` })
  await $.session.start({ cwd: WORKDIR, surface: null, isInteractive: false })
  await $.prompt.context(open())
  await $.tool.call({ tool: 'mcp__lore__remember', text: MARKER })
  await $.tool.call({ tool: 'mcp__lore__recall', query: MARKER })
  await $.tool.call({ tool: 'Write', file_path: INDEX_PATH, content: '- ' + 'q'.repeat(500) + ` ${MARKER} -> lore:x\n` })
  await $.tool.call({ tool: 'Bash', command: `echo ${MARKER} >> /p/memory/MEMORY.md` })
  expect(logs.length).toBeGreaterThan(0)
  expect(logs.join('\n')).not.toContain(MARKER)
})

test('/lore reports index size, over-cap lines, gardener and auto-memory per account without echoing content', async ($, on) => {
  const long = '- ' + 'w'.repeat(300) + ' -> lore:p'
  stubs(on, { index: `${INDEX_TEXT}${long}\n` })
  on('fs.list', () => ({ value: [{ name: 'report-2026-09-29.md', kind: 'file', size: 1, mtimeMs: 0, isLink: false }, { name: 'gardener.log', kind: 'file', size: 1, mtimeMs: 0, isLink: false }] }))
  const r = await $.command.run({ command: 'lore' })
  const text = String(r.text)
  expect(text).toContain('lore mod 0.1.0: active')
  expect(text).toContain('index: 4 lines')
  expect(text).toContain('over-cap lines: 1 at 4')
  expect(text).toContain('report-2026-09-29.md')
  expect(text).toContain('auto-memory .claude')
  expect(text).not.toContain('w'.repeat(50))
})

test('compaction is never blocked', async ($, on) => {
  stubs(on)
  const msgs = [{ role: 'user' as const, text: 'hello', toolUses: [] }]
  on('session.compact', () => ({ messages: msgs }))
  const r = await $.session.compact({ trigger: 'auto', messages: msgs })
  expect('skip' in r).toBe(false)
})

test('a subdirectory of a repo loads the repo root index, as the harness does', async ($, on) => {
  stubs(on, { cwd: WORKDIR + '/api/src', gitCommon: WORKDIR + '/.git' })
  expect(ctxOf(await $.prompt.context(open())).file.path).toBe(INDEX_PATH)
})

test('a git worktree loads the main checkout index', async ($, on) => {
  stubs(on, { cwd: '/home/user/Work/demo-b', gitCommon: WORKDIR + '/.git' })
  expect(ctxOf(await $.prompt.context(open())).file.path).toBe(INDEX_PATH)
})

test('outside git the cwd decides', async ($, on) => {
  stubs(on)
  expect(ctxOf(await $.prompt.context(open())).file.path).toBe(INDEX_PATH)
})
