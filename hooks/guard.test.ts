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
import { applyEdit, bashWritesIndex, check, isIndexPath } from './guard'

const LIMITS = { maxLines: 150, maxLineChars: 200, maxBytes: 19500 }
const A1 = '/home/user/.claude/projects/-home-user-Work-demo/memory/MEMORY.md'
const A3 = '/home/user/.claude-c/projects/-home-user-Work-demo/memory/MEMORY.md'

const pointer = (i: number) => `- Fact number ${i} hook -> lore:page-${i}`
const index = (n: number) => Array.from({ length: n }, (_, i) => pointer(i)).join('\n') + '\n'

// Runs one tool call through the loaded mod with MEMORY.md (or any file) holding `current`.
async function run($: any, on: any, path: string, args: Record<string, unknown>, current: string | null, tool = 'Edit') {
  const reads: string[] = []
  on('fs.read', (_$: any, e: any) => {
    reads.push(e.path)
    if (current === null) return { deny: 'ENOENT' }
    return { value: current }
  })
  on('ui.log', () => ({ value: undefined }))
  on('tool.call', () => ({ result: 'allowed' }))
  const r = await $.tool.call({ tool, file_path: path, ...args })
  return { r, reads }
}

test('pure: only MEMORY.md under a memory dir is an index path, in either account dir', () => {
  expect(isIndexPath(A1)).toBe(true)
  expect(isIndexPath(A3)).toBe(true)
  expect(isIndexPath('/home/u/.claude-b/projects/-x/memory/../memory/MEMORY.md')).toBe(true)
  expect(isIndexPath('/home/u/Work/proj/MEMORY.md')).toBe(false)
  expect(isIndexPath('/home/u/.claude/projects/-x/memory/topic.md')).toBe(false)
})

test('pure: shell writes are recognised, reads are not', () => {
  expect(bashWritesIndex('echo "- x -> lore:y" >> ~/.claude/projects/-x/memory/MEMORY.md')).toBe(true)
  expect(bashWritesIndex('sed -i "" s/a/b/ /p/memory/MEMORY.md')).toBe(true)
  expect(bashWritesIndex('printf x | tee /p/memory/MEMORY.md')).toBe(true)
  expect(bashWritesIndex('cp /tmp/new /p/memory/MEMORY.md')).toBe(true)
  expect(bashWritesIndex('cat /p/memory/MEMORY.md > /tmp/copy')).toBe(false)
  expect(bashWritesIndex('wc -l /p/memory/MEMORY.md')).toBe(false)
})

test('over-cap append is denied', async ($, on) => {
  const current = index(150)
  const { r } = await run($, on, A1, { old_string: pointer(149), new_string: pointer(149) + '\n' + pointer(150) }, current)
  expect(typeof r.deny).toBe('string')
  expect(r.deny).toContain('151 lines')
})

test('over-long line is denied', async ($, on) => {
  const { r } = await run($, on, A1, { content: index(10) + '- ' + 'x'.repeat(400) + ' -> lore:p\n' }, index(10), 'Write')
  expect(typeof r.deny).toBe('string')
  expect(r.deny).toContain('200 characters')
})

test('compliant pointer append is allowed', async ($, on) => {
  const { r } = await run($, on, A1, { old_string: pointer(9), new_string: pointer(9) + '\n- New hook -> lore:new-page' }, index(10))
  expect(r.result).toBe('allowed')
})

test('in-place update of a pointer line is allowed', async ($, on) => {
  const { r } = await run($, on, A1, { old_string: pointer(3), new_string: '- Fact number 3 reworded -> lore:page-3' }, index(10))
  expect(r.result).toBe('allowed')
})

test('a new list line without a pointer is denied', async ($, on) => {
  const { r } = await run($, on, A1, { old_string: pointer(9), new_string: pointer(9) + '\n- Detail that belongs in a page, not here' }, index(10))
  expect(typeof r.deny).toBe('string')
  expect(r.deny).toContain('no pointer')
})

test('unrelated files are untouched and never read', async ($, on) => {
  const { r, reads } = await run($, on, '/home/user/Work/demo/notes.md', { content: index(1000) }, null, 'Write')
  expect(r.result).toBe('allowed')
  expect(reads.length).toBe(0)
})

test('MEMORY.md in the other account dir is guarded too', async ($, on) => {
  const { r } = await run($, on, A3, { content: '- ' + 'y'.repeat(500) + ' -> lore:p\n' }, index(5), 'Write')
  expect(typeof r.deny).toBe('string')
})

test('rewrite of an oversize file that shrinks it is allowed', async ($, on) => {
  const oversize = index(200) + '- ' + 'z'.repeat(1200) + ' -> lore:old\n'
  const shrunk = index(180) + '- ' + 'z'.repeat(600) + ' -> lore:old\n'
  const { r } = await run($, on, A1, { content: shrunk }, oversize, 'Write')
  expect(r.result).toBe('allowed')
})

test('rewrite of an oversize file that grows it is denied', async ($, on) => {
  const oversize = index(200) + '- ' + 'z'.repeat(1200) + ' -> lore:old\n'
  const grown = index(220) + '- ' + 'z'.repeat(1200) + ' -> lore:old\n'
  const { r } = await run($, on, A1, { content: grown }, oversize, 'Write')
  expect(typeof r.deny).toBe('string')
})

test('a brand-new MEMORY.md must be compliant', async ($, on) => {
  const { r } = await run($, on, A1, { content: index(151) }, null, 'Write')
  expect(typeof r.deny).toBe('string')
})

test('an Edit whose old_string is absent is left to the tool', async ($, on) => {
  const { r } = await run($, on, A1, { old_string: 'not there', new_string: 'x' }, index(5))
  expect(r.result).toBe('allowed')
})

test('a read failure fails open', async ($, on) => {
  on('fs.read', () => ({ deny: 'boom' }))
  on('ui.log', () => ({ value: undefined }))
  on('tool.call', () => ({ result: 'allowed' }))
  const r = await $.tool.call({ tool: 'Edit', file_path: A1, old_string: 'a', new_string: 'b' })
  expect(r.result).toBe('allowed')
})

test('applyEdit handles MultiEdit sequentially', () => {
  const out = applyEdit('a\nb\n', { tool: 'MultiEdit', edits: [{ old_string: 'a', new_string: 'c' }, { old_string: 'c', new_string: 'd' }] })
  expect(out).toBe('d\nb\n')
  expect(check('a\n', 'b\n', LIMITS)).toEqual({ ok: true })
})

test('bash write to MEMORY.md is denied through the mod, a read passes', async ($, on) => {
  on('ui.log', () => ({ value: undefined }))
  on('tool.call', () => ({ result: 'allowed' }))
  const denied = await $.tool.call({ tool: 'Bash', command: 'echo "- x -> lore:y" >> /p/.claude/projects/-x/memory/MEMORY.md' })
  expect(typeof denied.deny).toBe('string')
  const ok = await $.tool.call({ tool: 'Bash', command: 'wc -l /p/.claude/projects/-x/memory/MEMORY.md' })
  expect(ok.result).toBe('allowed')
})
