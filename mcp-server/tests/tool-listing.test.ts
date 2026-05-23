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
  test("lists every read, write, and memory tool", () => {
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
      "search_memory",
      "search_notes",
      "set_agent_state",
    ].sort();

    expect(names).toEqual(expected);
  });

  test("write tools appear in the listing unconditionally", () => {
    // MCP clients like Claude Code cache tool discovery at session start and
    // never re-fetch; if writes ever stopped being listed, those clients would
    // permanently lose the ability to call them.
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

  test("no request_capabilities meta-tool is exposed", () => {
    // The pre-#72 design required agents to call request_capabilities before
    // any write. It provided no security (unauthenticated) and was removed —
    // validateOwner in agent-identity.ts is the real authorization layer.
    const names = listAllTools(testConfig()).map((t) => t.name);
    expect(names).not.toContain("request_capabilities");
  });

  test("vault-write tools mark `owner` as required in inputSchema (#63)", () => {
    // Regression guard: validateOwner enforces identity at the data layer,
    // but agents discover required fields from the inputSchema. If `owner`
    // silently drops out of `required[]` in a future refactor, agents
    // would stop passing it and every call would fail at runtime with
    // CONFIG_ERROR / VALIDATION_ERROR — wasting tokens on an avoidable
    // round-trip. This test catches that drift at build time.
    const tools = listAllTools(testConfig());
    for (const name of ["create_note", "add_connection", "assign_domain"]) {
      const tool = tools.find((t) => t.name === name);
      expect(tool).toBeDefined();
      const required = (tool!.inputSchema as { required?: string[] }).required ?? [];
      expect(required).toContain("owner");
    }
  });
});
