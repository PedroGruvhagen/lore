# Karpathy LLM Wiki Pattern

> Based on Andrej Karpathy's LLM Wiki pattern (published April 3, 2026)

## Core Idea

Large language models have a fundamental problem with factual currency: their training data becomes stale the moment training ends, but they answer with full confidence regardless. Traditional RAG (retrieval-augmented generation) addresses this by searching external documents at query time, but RAG is fragile, requires careful chunking and embedding, and adds latency to every interaction.

Karpathy proposed a simpler alternative: instead of searching at query time, have the LLM itself build and maintain a persistent wiki of facts it needs to know. The wiki is compiled once from authoritative sources and then kept current through periodic maintenance passes. Because the LLM is both author and consumer of the wiki, the format naturally matches what it can parse and use effectively.

The key insight is that maintenance cost is near zero because the LLM handles all the bookkeeping: reading sources, updating pages, rebuilding indexes, and checking for staleness. A human only needs to seed the initial topics and occasionally verify that the system is working. Over time, the wiki compounds in value as more facts are added and cross-referenced.

## How This Skill Adapts It

The `lore` skill is a concrete instantiation of Karpathy's pattern, designed specifically for Claude Code. It adapts the abstract idea into a working system with:

- **Two-layer architecture**: A global lore instance (`~/.claude/lore/` by default) holds cross-project facts (API versions, model names, best practices), while optional per-project lore (`{project}/lore/`) holds project-specific context (architecture decisions, client requirements, ingested research). Global lore is shared across all projects and machines; project lore travels with the repo.

- **Enforcement via CLAUDE.md**: The mandatory lookup rule in CLAUDE.md ensures Claude always checks lore before answering technical questions. This is not optional behavior; it is enforced by the instruction set. Lore overrides training data for all factual technical claims.

- **Five operations**: Ingest (add new knowledge), Query (search and synthesize), Lint (find problems), Refresh (update stale pages from the web), and Auto-Patch (propose CLAUDE.md corrections based on lore). Each operation is a defined workflow with clear steps.

- **Focus on technical facts that Claude's training data gets wrong**: Model IDs, API versions, SDK methods, pricing, deprecation status, LTS schedules, packaging best practices. These are the facts that change fastest and cause the most real-world errors.

## Note

The original idea file is intentionally abstract. This skill is a concrete instantiation for Claude Code, with specific file formats, CLI tooling, enforcement rules, and a two-layer architecture that the original proposal does not specify.
