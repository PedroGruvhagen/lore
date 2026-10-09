# Page format

The authoritative page format spec ships with the skill itself:
[`skill/references/page-format.md`](../skill/references/page-format.md). This document is a
short map of it, not a copy; the frontmatter fields, category list, and the complete example
page live in that file so there is exactly one place to keep them in sync.

## What is in `skill/references/page-format.md`

- The YAML frontmatter block every page starts with, and its field reference: `title`,
  `category`, `auto_update`, `last_verified`, `refresh_interval`, `confidence`, `sources`,
  `tags`, with which are required and what each one defaults to.
- The six valid `category` values (`api-reference`, `best-practices`, `deprecations`,
  `tooling`, `infrastructure`, `custom`).
- Body conventions: a one-sentence summary as the first line after the frontmatter,
  `##` section headers, bold for facts that change over time, `[[wikilink]]` and
  `[[global:slug]]` cross-references, a `## Recent Changes` section, and the 200-line length
  guideline: `lore.py lint` warns `[oversized] pages/<slug>.md: <n> body lines (cap 200;
  curate on next touch, move chronicles to project lore)` for any page over that length.
- A complete example page using fictional model names, which `examples/pages/example-api.md`
  in this repository follows.

## Where it is read from

`lore.py bootstrap` does not copy this file into a new lore directory; it writes `schema.md`
(described in `architecture.md`) with a short structural summary instead. The full page
format spec lives once, in the skill itself, and is the reference an agent maintaining a
lore directory should read before writing a page, the same file this document points at.

## Writing your first page

`examples/pages/example-api.md` is a complete, valid page you can copy as a starting point:
valid frontmatter, a one-sentence summary, section headers, a fictional model table, and a
`## Recent Changes` entry. `examples/quickstart.sh` copies that same page into a fresh
bootstrapped lore directory and runs it through `lore.py index`, `lore.py search`,
`lore.py fact`, and `lore.py lint` end to end.
