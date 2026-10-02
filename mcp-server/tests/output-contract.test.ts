import Ajv from "ajv";
import { listAllTools } from "../src/tool-registry.js";
import { formatToolResult } from "../src/output-contract.js";
import type { VaultConfig } from "../src/types.js";

const config: VaultConfig = {
  name: "test",
  path: "/tmp/irrelevant",
  directories: ["notes", "concepts"],
  connectionTypes: ["extends"],
  statuses: ["draft", "final"],
  writeBranch: "drafts",
};

const tools = listAllTools(config);
const ajv = new Ajv();

test("every advertised tool has a compilable output schema", () => {
  expect(tools).toHaveLength(19);
  for (const tool of tools) {
    expect(tool.outputSchema).toBeDefined();
    expect(() => ajv.compile(tool.outputSchema)).not.toThrow();
  }
});

test("search results retain legacy text and provide schema-valid structured data", () => {
  const result = {
    results: [{
      id: "notes/example.md", title: "Example", date: "2026-09-24",
      status: null, tags: ["example"], snippet: "A note",
    }],
    cursor: "next-page",
  };
  const response = formatToolResult("search_notes", result);
  const schema = tools.find((tool) => tool.name === "search_notes")!.outputSchema;

  expect(response.content[0].text).toBe(JSON.stringify(result, null, 2));
  expect("structuredContent" in response && response.structuredContent).toEqual(result);
  expect(ajv.compile(schema)("structuredContent" in response && response.structuredContent)).toBe(true);
});

test("missing agent state is structured without changing the legacy null text", () => {
  const response = formatToolResult("get_agent_state", null);
  const schema = tools.find((tool) => tool.name === "get_agent_state")!.outputSchema;

  expect(response.content[0].text).toBe("null");
  expect("structuredContent" in response && response.structuredContent).toEqual({ state: null });
  expect(ajv.compile(schema)("structuredContent" in response && response.structuredContent)).toBe(true);
});

test("present agent state is wrapped exactly once and validates against the schema (#678)", () => {
  // The null case cannot see a double wrap: {state: {state: null}} fails nothing
  // that {state: null} passes. Only a real entry makes the nesting observable.
  const entry = {
    key: "agent1.session", value: { note: "active" },
    owner: "agent1", updated_at: "2026-09-28T00:00:00.000Z", ttl_hours: null,
  };
  const response = formatToolResult("get_agent_state", entry);
  const schema = tools.find((tool) => tool.name === "get_agent_state")!.outputSchema;
  const structured = "structuredContent" in response ? response.structuredContent : undefined;

  expect(response.content[0].text).toBe(JSON.stringify(entry, null, 2));
  expect(structured).toEqual({ state: entry });
  expect((structured as { state: { key?: string } }).state.key).toBe("agent1.session");
  const validate = ajv.compile(schema);
  expect({ ok: validate(structured), errors: validate.errors }).toEqual({ ok: true, errors: null });
});

test("tool errors retain their payload and are marked as errors", () => {
  const result = { error: "NOT_FOUND", message: "Note not found" };
  const response = formatToolResult("get_note", result);

  expect(response).toEqual({
    isError: true,
    content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
  });
  expect("structuredContent" in response).toBe(false);
});
