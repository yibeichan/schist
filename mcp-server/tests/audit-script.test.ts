import { describe, it, expect } from "@jest/globals";
import { measureResponse } from "../../scripts/audit_mcp_response_sizes.js";

describe("measureResponse", () => {
  it("returns byte length of JSON-serialized response", () => {
    const result = measureResponse({ id: "x", title: "y", snippet: "z" });
    // {"id":"x","title":"y","snippet":"z"} = 36 bytes
    expect(result.bytes).toBe(36);
  });

  it("returns approximate token count using 4-bytes-per-token heuristic", () => {
    const result = measureResponse({ a: "x".repeat(40) });
    // {"a":"xxxx...xxxx"} = 48 bytes ≈ 12 tokens
    expect(result.approxTokens).toBe(12);
  });

  it("handles array responses (e.g. searchNotes return)", () => {
    const result = measureResponse([{ id: "a" }, { id: "b" }]);
    // [{"id":"a"},{"id":"b"}] = 23 bytes
    expect(result.bytes).toBe(23);
    expect(result.entryCount).toBe(2);
  });

  it("reports entryCount: 1 for non-array responses", () => {
    const result = measureResponse({ noteCount: 0 });
    expect(result.entryCount).toBe(1);
  });
});
