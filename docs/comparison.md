# schist vs alternatives

schist is for teams and individual operators who want AI agents to keep durable,
queryable working memory in a local-first repository. It is not a general notes
app, a hosted workspace, or a vector database. Its strongest fit is structured
agent memory where git history, markdown reviewability, and indexed graph search
matter more than polished human editing surfaces.

## Quick fit check

Use schist when you want:

- Agents to read and write notes through MCP or CLI tools.
- Knowledge stored as markdown in git, with normal branch and PR workflows.
- Full-text search, graph traversal, and concept links without running a hosted
  service.
- Multi-machine sync using git, with hub-side ACL checks for agent writes.
- A read-only static viewer for humans while agents remain the primary writers.

Do not use schist as the primary system when you need:

- A mobile-first or rich web editor.
- Real-time collaborative editing.
- Fine-grained page permissions for human workspaces.
- Turnkey hosted sync, notifications, comments, or task management.
- Semantic similarity search as the only retrieval model.

## Comparison table

| Tool | Best for | Strengths | Tradeoffs vs schist |
|------|----------|-----------|---------------------|
| schist | Agent-written knowledge graphs and persistent memory | MCP-first, git-native, local-first, SQLite FTS and graph queries, append-oriented audit trail | Young project, no rich editor, no mobile app, no real-time sync |
| Obsidian | Personal human notes and linked thinking | Excellent editor ecosystem, local markdown, strong plugin community | Agent integration is indirect; graph and metadata conventions are mostly human-maintained |
| Notion | Collaborative workspace docs and lightweight databases | Polished web UI, sharing, comments, databases, team adoption | Cloud-first, proprietary data model, API is not designed as an agent memory layer |
| Logseq | Outliner-based personal knowledge management | Blocks, backlinks, daily notes, local files | Human workflow comes first; agent writes need conventions outside the core product |
| Mem.ai | AI-assisted personal knowledge capture | Low-friction capture and recall, hosted AI features | Less transparent storage and versioning; weaker fit for repo-reviewed agent output |
| Raw markdown folders | Simple local notes | Minimal, portable, no infrastructure | Search is usually grep; no built-in graph schema, MCP surface, or write discipline |
| Vector databases | Semantic retrieval over large corpora | Strong fuzzy recall and embedding search | Not a source of truth; weak human reviewability and poor fit for append-only decisions |

## Where schist is different

schist treats the vault as a software artifact. Notes are plain markdown with
YAML frontmatter, commits are the durable log, and SQLite is a disposable query
layer rebuilt from the files. That means an agent can create a note, commit it,
and later another agent can search or query it without trusting a chat transcript
or a hidden embedding store.

This also shapes the product boundaries. schist deliberately avoids becoming a
new hosted notes app. The user interface is read-only and static. Editing happens
through MCP tools, CLI commands, or normal git workflows. That is a strength for
agent accountability and a weakness for users who primarily want a polished human
authoring environment.

## When to choose alternatives

Choose Obsidian or Logseq if your main workflow is human note-taking and you want
a rich daily writing surface, backlinks, plugins, and keyboard-driven editing.
schist can coexist with those tools, but it is not trying to replace their
editing experience.

Choose Notion if the center of gravity is team collaboration: comments, shared
pages, lightweight project management, and non-technical contributors. schist's
git-based workflow is more transparent for agents but less approachable for
general workspace users.

Choose raw markdown folders if you only need a small local notebook and grep is
good enough. schist adds structure, indexing, schemas, MCP tools, and sync rules;
those are useful once agents and multiple machines are involved, but unnecessary
for simple personal notes.

Choose a vector database when semantic similarity over a large corpus is the core
problem and source-of-truth reviewability is secondary. schist may eventually add
embedding-backed search, but its foundation is still explicit files, metadata,
and graph edges.

## Current limitations

- No real-time sync. Multi-machine sync is git-based hub and spoke, not live
  collaborative editing.
- No web or mobile editor. The viewer is static and read-only.
- No hosted service. You run the CLI, MCP server, vault, and optional viewer
  yourself.
- No first-class semantic search yet. Retrieval is currently based on SQLite FTS,
  graph queries, concepts, and metadata.
- No broad third-party integrations beyond MCP, CLI, git, and the static viewer.

These limitations are intentional enough to keep schist small, local-first, and
reviewable. They are also real product tradeoffs. If the missing surface is the
main thing you need, another tool will be a better fit.
