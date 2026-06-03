import { listAllTools, REMOVED_TOOLS } from "../src/tool-registry.js";
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
      "create_note",
      "delete_agent_state",
      "get_agent_state",
      "get_context",
      "get_note",
      "list_concepts",
      "query_graph",
      "search_memory",
      "search_notes",
      "set_agent_state",
      "sync_retry",
      "sync_status",
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
      "sync_retry",
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

  test("removed tools have a tombstone and are not also listed as live", () => {
    // A removed tool must guide callers (stale skills, cached clients) instead
    // of hitting the bare "Unknown tool" path — but it must never reappear in
    // the live listing, or clients would try to call a dead tool.
    const names = listAllTools(testConfig()).map((t) => t.name);
    for (const removed of Object.keys(REMOVED_TOOLS)) {
      expect(names).not.toContain(removed);
      expect(REMOVED_TOOLS[removed].length).toBeGreaterThan(0);
    }
    // request_capabilities specifically: the symptom that motivated the
    // tombstone was skills calling it and getting an unactionable error.
    expect(REMOVED_TOOLS).toHaveProperty("request_capabilities");
  });

  test("tombstone lookup must use own-property check, not `in`", () => {
    // The router dispatches an unknown tool to the REMOVED_TOOLS tombstone.
    // `name` is client-controlled, so `name in REMOVED_TOOLS` would match
    // inherited Object.prototype members ("constructor", "toString",
    // "__proto__", "valueOf", "hasOwnProperty") and return a malformed
    // TOOL_REMOVED (the value is a function, dropped by JSON.stringify).
    // index.ts must use Object.hasOwn; this guards the contract it relies on.
    for (const polluted of ["constructor", "toString", "__proto__", "valueOf", "hasOwnProperty"]) {
      expect(polluted in REMOVED_TOOLS).toBe(true); // the footgun
      expect(Object.hasOwn(REMOVED_TOOLS, polluted)).toBe(false); // the fix
    }
    expect(Object.hasOwn(REMOVED_TOOLS, "request_capabilities")).toBe(true);
  });

  test("vault-write tools mark `owner` as required in inputSchema (#63)", () => {
    // Regression guard: validateOwner enforces identity at the data layer,
    // but agents discover required fields from the inputSchema. If `owner`
    // silently drops out of `required[]` in a future refactor, agents
    // would stop passing it and every call would fail at runtime with
    // CONFIG_ERROR / VALIDATION_ERROR — wasting tokens on an avoidable
    // round-trip. This test catches that drift at build time.
    const tools = listAllTools(testConfig());
    for (const name of ["create_note", "add_connection"]) {
      const tool = tools.find((t) => t.name === name);
      expect(tool).toBeDefined();
      const required = (tool!.inputSchema as { required?: string[] }).required ?? [];
      expect(required).toContain("owner");
    }
  });
});
