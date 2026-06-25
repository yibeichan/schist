# Frontmatter Parser Parity

schist has two frontmatter readers:

- Python ingest (`cli/schist/ingest.py`) for Markdown-to-SQLite rebuilds.
- TypeScript `parseNote` (`mcp-server/src/markdown-parser.ts`) for MCP note edits.

Both readers must accept the same frontmatter for legacy notes that contain
unquoted hashtag tokens in YAML flow collections, such as `tags: [ #foo ]` or
`extra: { category: #foo }`.
The canonical behavior is pinned in `schema/frontmatter-parser-parity.json` and
exercised by both the CLI pytest suite and the MCP Jest suite.
Unquoted hashtag values in flow collections are quoted from `#` through the
next whitespace or flow delimiter, so identifier-like values such as
`#cs.AI.1234` stay intact.

Decision for depth-zero trailing hashtags: outside an actual YAML flow sequence,
`#` starts a YAML comment. For example, `title: read [book] about #life` parses
as `read [book] about`; the parser must not "rescue" `#life` into the title just
because the line contains a bracket.
