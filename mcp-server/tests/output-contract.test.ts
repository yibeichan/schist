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

test("tool errors retain their payload and are marked as errors", () => {
  const result = { error: "NOT_FOUND", message: "Note not found" };
  const response = formatToolResult("get_note", result);

  expect(response).toEqual({
    isError: true,
    content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
  });
  expect("structuredContent" in response).toBe(false);
});
