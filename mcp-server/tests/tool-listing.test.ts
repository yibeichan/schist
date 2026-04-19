import { listAllTools } from "../src/tool-registry.js";
import type { VaultConfig } from "../src/types.js";

function testConfig(): VaultConfig {
  return {
    name: "test",
    path: "/tmp/irrelevant",
    directories: ["notes", "papers"],
    connectionTypes: ["extends", "supports"],
    statuses: ["draft", "final"],
    writeBranch: "drafts",
  };
}

describe("listAllTools — unconditional tool exposure", () => {
  test("lists every read, write, memory, and capability tool", () => {
    const names = listAllTools(testConfig()).map((t) => t.name).sort();

    const expected = [
      "add_concept_alias",
      "add_connection",
      "add_memory",
      "assign_domain",
      "create_note",
      "delete_agent_state",
      "get_agent_state",
      "get_context",
      "get_note",
      "list_concepts",
      "list_domains",
      "query_graph",
      "request_capabilities",
      "search_memory",
      "search_notes",
      "set_agent_state",
    ].sort();

    expect(names).toEqual(expected);
  });

  test("write tools appear in the listing even without prior capability unlock", () => {
    // listAllTools has no knowledge of writeEnabled state — it is invoked by
    // the ListTools handler unconditionally. If this assertion ever regresses,
    // MCP clients that cache tool discovery (Claude Code) will silently lose
    // the ability to call any write tool.
    const names = listAllTools(testConfig()).map((t) => t.name);

    for (const tool of [
      "create_note",
      "add_connection",
      "add_memory",
      "set_agent_state",
      "delete_agent_state",
      "add_concept_alias",
    ]) {
      expect(names).toContain(tool);
    }
  });

  test("request_capabilities description mentions invocation gate, not listing gate", () => {
    const reqCap = listAllTools(testConfig()).find((t) => t.name === "request_capabilities");
    expect(reqCap).toBeDefined();
    // The description must convey that the gate is at call time, not list time.
    expect(reqCap!.description?.toLowerCase()).toMatch(/invocation|calls? to|succeed/);
  });
});
