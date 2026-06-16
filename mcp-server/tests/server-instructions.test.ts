import { readFileSync } from "fs";
import path from "path";
import { SERVER_INSTRUCTIONS } from "../src/server-instructions.js";

describe("MCP server instructions", () => {
  test("tell clients to prefer indexed schist tools over filesystem search", () => {
    expect(SERVER_INSTRUCTIONS).toContain("prefer search_notes");
    expect(SERVER_INSTRUCTIONS).toContain("search_memory");
    expect(SERVER_INSTRUCTIONS).toContain("query_graph");
    expect(SERVER_INSTRUCTIONS).toContain("get_context");
    expect(SERVER_INSTRUCTIONS).toContain("over filesystem grep/find");
    expect(SERVER_INSTRUCTIONS).toContain("Use create_note and add_memory");
  });

  test("are wired into the MCP Server options", () => {
    const indexSource = readFileSync(path.join(process.cwd(), "src", "index.ts"), "utf8");

    expect(indexSource).toContain("instructions: SERVER_INSTRUCTIONS");
  });
});
