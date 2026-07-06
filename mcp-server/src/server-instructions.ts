export const SERVER_INSTRUCTIONS =
  "schist is the knowledge-vault server. When searching or recalling vault content " +
  "(notes, memory entries, concepts, papers), prefer search_notes, search_memory, " +
  "query_graph, and get_context over filesystem grep/find because they use the " +
  "indexed graph, respect scopes, and return snippets with stable note ids. Use " +
  "filesystem tools only for structural questions the index cannot answer, such as " +
  "directory layout, line counts, or symlinks. Use create_note and add_memory to " +
  "persist new knowledge instead of writing vault or memory files directly. " +
  // Memory-vs-note decision boundary (docs/data-model.md D4, slice C).
  "Choose the store deliberately: add_memory is for frequent, small, " +
  "session-scoped facts — decisions made, blockers hit, current working state; " +
  "create_note is for durable, curated, cross-session knowledge that belongs in " +
  "the git-backed vault. When a memory entry proves useful beyond the session " +
  "that wrote it, graduate it into a vault note, and set the memory entry's " +
  "related_doc to a vault note id (notes/….md) when it concerns a specific note.";
