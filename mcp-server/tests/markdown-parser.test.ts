import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseNote, buildNote, buildConnectionLine, parseConnections } from "../src/markdown-parser.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type FrontmatterParityCase = {
  name: string;
  frontmatter: string;
  expected: Record<string, unknown>;
};

function loadFrontmatterParityCases(): FrontmatterParityCase[] {
  const fixturePath = path.resolve(__dirname, "..", "..", "schema", "frontmatter-parser-parity.json");
  return JSON.parse(readFileSync(fixturePath, "utf-8")) as FrontmatterParityCase[];
}

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

  test("Connection injection (#398): NON-\\n line boundaries in context cannot forge an edge", () => {
    // sanitizeContext once flattened only "\n". Every other codepoint
    // splitLinesLikePython splits on (\r \v \f \x1c \x1d \x1e \x85 U+2028
    // U+2029) survived, and the `^…/gm` prefix strip anchors only at JS line
    // terminators — so a payload separated by e.g. \v, or a double
    // `- a: - extends: evil` payload split by \r, forged a second edge on read.
    const boundaries = ["\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", " ", " "];
    for (const b of boundaries) {
      // single-prefix payload (defeats the non-anchored boundaries) and a
      // double-prefix payload (defeats the strip for anchored boundaries).
      for (const payload of [
        `ctx${b}- extends: notes/hijacked.md`,
        `ctx${b}- a: - extends: notes/hijacked.md${b}tail`,
      ]) {
        const line = buildConnectionLine({ target: "notes/legit.md", type: "extends", context: payload });
        const parsed = parseConnections(`## Connections\n${line}\n`);
        expect(parsed).toHaveLength(1);
        expect(parsed[0].target).toBe("notes/legit.md");
        expect(parsed.some((c) => c.target === "notes/hijacked.md")).toBe(false);
      }
    }
  });

  test("Quote injection: embedded \" in context is escaped and cannot break parseConnections", () => {
    // A crafted context with a closing quote + fake connection line
    const maliciousContext = 'legit" "fake: injection.md "injected context';
    const conn = { target: "notes/target.md", type: "extends", context: maliciousContext };
    const line = buildConnectionLine(conn);

    // The line must remain a single line
    expect(line).not.toContain("\n");

    // Parsing the output must produce exactly one connection, not more
    const parsed = parseConnections(`## Connections\n${line}\n`);
    expect(parsed).toHaveLength(1);
    expect(parsed[0].target).toBe("notes/target.md");
    expect(parsed[0].type).toBe("extends");
  });

  test("Quote injection: bare double-quote in context is replaced with single-quote", () => {
    const conn = {
      target: "notes/a.md",
      type: "supports",
      context: 'he said "hello" to her',
    };
    const line = buildConnectionLine(conn);
    // Double-quotes replaced with single-quotes so the delimited field isn't broken
    expect(line).toContain("'hello'");
    expect(line).not.toContain('"hello"');
    // Should still parse back as one connection
    const parsed = parseConnections(`## Connections\n${line}\n`);
    expect(parsed).toHaveLength(1);
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

  test.each(loadFrontmatterParityCases())(
    "parseNote matches shared frontmatter parity fixture: $name",
    ({ frontmatter, expected }) => {
      const parsed = parseNote(`---\n${frontmatter}\n---\n\nBody\n`);

      for (const [key, value] of Object.entries(expected)) {
        expect(parsed.metadata[key]).toEqual(value);
      }
    }
  );

  test("parseNote accepts unquoted hashtag values in frontmatter flow sequences", () => {
    const parsed = parseNote(
      "---\n" +
      "title: Tagged Note\n" +
      "tags: [ #foo, #bar-baz, already-plain ]\n" +
      "concepts: [ #concept ]\n" +
      "---\n\n" +
      "Body\n"
    );

    expect(parsed.metadata.tags).toEqual(["#foo", "#bar-baz", "already-plain"]);
    expect(parsed.metadata.concepts).toEqual(["#concept"]);
    expect(parsed.body).toContain("Body");
  });

  test("parseNote leaves already quoted hashtag flow values intact", () => {
    const parsed = parseNote(
      "---\n" +
      "title: Quoted Tags\n" +
      "tags: [ \"#foo\", '#bar' ]\n" +
      "---\n\n" +
      "Body\n"
    );

    expect(parsed.metadata.tags).toEqual(["#foo", "#bar"]);
  });

  // Bare scalars with apostrophes on the same flow line as an unquoted hashtag
  // must not desync quote tracking (an apostrophe mid-scalar is not a quote
  // opener). ingest accepts these; parseNote must too. Regression for #260.
  test("parseNote handles apostrophes in bare flow scalars alongside hashtags", () => {
    const parsed = parseNote(
      "---\n" +
      "tags: [ it's, can't, #foo ]\n" +
      "---\n\n" +
      "Body\n"
    );

    expect(parsed.metadata.tags).toEqual(["it's", "can't", "#foo"]);
  });

  // Quoted scalars whose content ends in an escaped backslash (\\) or contains
  // an escaped quote (\") must close at the right place so the trailing hashtag
  // is still quoted. The naive single-char lookback got this wrong.
  test("parseNote handles escaped backslashes and quotes in flow scalars", () => {
    const parsed = parseNote(
      "---\n" +
      "tags: [ \"ends\\\\\", \"a\\\"b\", 'it''s', #foo ]\n" +
      "---\n\n" +
      "Body\n"
    );

    expect(parsed.metadata.tags).toEqual(["ends\\", "a\"b", "it's", "#foo"]);
  });

  // Shared parser-parity decision (#263): a bare `[` in a plain scalar with a
  // trailing `# token` is not a flow sequence, so the hashtag stays at
  // flowDepth 0 and YAML treats it as a comment (dropped). Python ingest and TS
  // parseNote both pin this behavior via schema/frontmatter-parser-parity.json.
  test("parseNote drops a depth-0 trailing hashtag comment (documented parity decision)", () => {
    const parsed = parseNote(
      "---\n" +
      "title: read [book] about #life\n" +
      "---\n\n" +
      "Body\n"
    );

    expect(parsed.metadata.title).toBe("read [book] about");
  });
});
