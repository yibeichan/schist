export const SERVER_INSTRUCTIONS =
  "schist is the knowledge-vault server. When searching or recalling vault content " +
  "(notes, memory entries, concepts, papers), prefer search_notes, search_memory, " +
  "query_graph, and get_context over filesystem grep/find because they use the " +
  "indexed graph, respect scopes, and return snippets with stable note ids. Use " +
  "filesystem tools only for structural questions the index cannot answer, such as " +
  "directory layout, line counts, or symlinks. Use create_note and add_memory to " +
  "persist new knowledge instead of writing vault or memory files directly.";
