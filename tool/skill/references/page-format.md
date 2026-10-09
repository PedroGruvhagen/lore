# Lore Page Format Specification

Every page in the lore knowledge base is a markdown file with YAML frontmatter and a structured body. This document is the authoritative reference for the format.

## Frontmatter

All pages begin with a YAML frontmatter block delimited by `---` lines.

```yaml
---
title: "Page Title"
category: "api-reference"
auto_update: true
last_verified: "2026-04-08"
refresh_interval: "5d"
confidence: "high"
sources:
  - "https://docs.example.com/api"
  - "/path/to/local/file.md"
tags:
  - example
  - api
---
```

### Field Reference

| Field | Required | Type | Default | Description |
|-------|----------|------|---------|-------------|
| `title` | Yes | string | - | Human-readable page title. Used in index.md and search results. |
| `category` | Yes | string | - | One of the valid categories (see below). |
| `auto_update` | No | boolean | `false` | If `true`, this page is eligible for automatic refresh via web search. Pages with `auto_update: true` can also be used by auto-patch to propose CLAUDE.md corrections. |
| `last_verified` | Yes | string | - | ISO 8601 date (`YYYY-MM-DD`) when the page content was last verified against its sources. Set to `""` for unverified templates. |
| `refresh_interval` | No | string | `"30d"` | How often to re-verify. Format: number + `d` (days). Examples: `"5d"`, `"7d"`, `"30d"`. |
| `confidence` | No | string | `"medium"` | Confidence level: `"high"` (multiple sources, recently verified), `"medium"` (single source or not recently verified), `"low"` (inferred, unverified, or stale). |
| `sources` | Yes | list | - | At least one URL or file path. These are the authoritative sources for the page content. Sources are immutable once set (add new ones, never remove). |
| `tags` | No | list | `[]` | Free-form tags for search and filtering. Lowercase, hyphenated. |

### Valid Categories

| Category | Use for |
|----------|---------|
| `api-reference` | API endpoints, model IDs, SDK versions, pricing |
| `best-practices` | Current recommended approaches, tooling, patterns |
| `deprecations` | Deprecated models, APIs, patterns, and their replacements |
| `tooling` | CLI tools, build tools, package managers |
| `infrastructure` | Servers, services, deployment, CI/CD |
| `custom` | Anything that doesn't fit the above |

## Body Conventions

### First Line: Summary

The first line after the frontmatter closing `---` must be a one-sentence summary of the page. This line is extracted by the index builder and displayed in `index.md`.

```markdown
---
...frontmatter...
---
Current Anthropic API model IDs, context windows, pricing, and SDK versions.
```

### Section Headers

Use `##` headers for major sections. Keep sections focused.

```markdown
## Current Models
## SDKs
## Pricing
## Recent Changes
```

### Key Facts

Use **bold** for key facts that change over time: model names, version numbers, dates, prices.

```markdown
The current flagship model is **Example Model Large** (ID: **example-model-large**).
```

### Cross-References

Link to other lore pages using double-bracket syntax:

```markdown
See [[deprecated-patterns]] for models that have been sunset.
For project-specific context, check [[global:anthropic-api]] from project lore.
```

- `[[page-slug]]` links to a page in the same lore scope (global or project).
- `[[global:page-slug]]` explicitly links to global lore from a project page.

### Recent Changes Section

Every page should include a `## Recent Changes` section at the bottom, listing dated changes:

```markdown
## Recent Changes

- **2026-04-08**: Initial page creation with current model data.
- **2026-04-01**: Example Model Large released, replacing the previous generation as flagship.
```

### Length Limit

Keep pages under **200 lines**. If a topic grows larger, split it into multiple focused pages and cross-reference them.

## Complete Example

```markdown
---
title: "Anthropic API"
category: "api-reference"
auto_update: true
last_verified: "2026-04-08"
refresh_interval: "5d"
confidence: "high"
sources:
  - "https://docs.anthropic.com/en/docs/about-claude/models"
  - "https://docs.anthropic.com/en/api/getting-started"
tags:
  - anthropic
  - claude
  - api
  - models
---
Current Anthropic API model IDs, context windows, pricing, and SDK versions.

## Current Models

| Model | ID | Context | Max Output |
|-------|----|---------|------------|
| Example Model Large | example-model-large | 200K | 32K |
| Example Model Mid | example-model-mid | 200K | 64K |
| Example Model Small | example-model-small-20260101 | 200K | 8K |

## SDKs

- **Python**: `anthropic` v0.52+ (`pip install anthropic`)
- **JavaScript/TypeScript**: `@anthropic-ai/sdk` v0.39+ (`npm install @anthropic-ai/sdk`)

## Pricing

See source docs for current pricing. Prices change frequently.

## Recent Changes

- **2026-04-08**: Initial page creation.
```
