import { parseNote, buildNote, buildConnectionLine, parseConnections } from "../src/markdown-parser.js";

describe("markdown-parser", () => {
  test("Connection injection: newline in context is stripped to single line", () => {
    const conn = {
      target: "notes/foo.md",
      type: "supports",
      context: "safe context\n- supports: evil/path.md",
    };
    const line = buildConnectionLine(conn);
    expect(line).not.toContain("\n");
    expect(line).not.toMatch(/\n-\s+\S+:/);
    expect(line).toContain("notes/foo.md");
    expect(line).toContain("safe context");
  });

  test("Round-trip: parseNote(buildNote(meta, body, conns)) matches original", () => {
    const metadata: Record<string, unknown> = {
      title: "Test Note",
      date: "2026-03-27",
      tags: ["test", "roundtrip"],
      status: "draft",
    };
    const body = "This is the body content.\n\nSome more text.";
    const connections = [
      { target: "notes/other.md", type: "supports", context: "test context" },
      { target: "concepts/idea.md", type: "extends" },
    ];

    const built = buildNote(metadata, body, connections);
    const parsed = parseNote(built);

    expect(parsed.metadata.title).toBe(metadata.title);
    expect(parsed.metadata.date).toBe(metadata.date);
    expect(parsed.metadata.status).toBe(metadata.status);

    expect(parsed.connections.length).toBe(connections.length);
    expect(parsed.connections[0].target).toBe(connections[0].target);
    expect(parsed.connections[0].type).toBe(connections[0].type);
    expect(parsed.connections[0].context).toBe(connections[0].context);
    expect(parsed.connections[1].target).toBe(connections[1].target);
    expect(parsed.connections[1].type).toBe(connections[1].type);
  });
});
