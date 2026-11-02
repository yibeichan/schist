// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * TS CONNECTION_RE + parseConnections (markdown-parser.ts) and Python
 * CONNECTION_RE + parse_connections (ingest.py) must agree at BOTH levels:
 *   - `lines`  — per-line regex parity (#338)
 *   - `bodies` — whole-body line-SPLITTER parity (#359): Python ingest
 *                iterates body.splitlines() (breaks on CR/VT/FF/FS/GS/RS/NEL/
 *                LS/PS, not just \n), so a TS reader that split only on \n let
 *                a bogus type smuggled across such a separator bypass the #317
 *                vocabulary check while ingest still indexed the edge.
 * Python's parse builds the index; TS's parse drives get_note/query_graph,
 * delete_note's cascade, and write-time validation — any skew silently
 * diverges the graph from what the tools believe is in it.
 * schema/connection-line-parity.json is the single source of truth, consumed
 * here and by cli/tests/test_ingest.py. Modeled on the #318 concept-slug contract.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import {
  CONNECTION_RE,
  connectionBody,
  normalizeNewlines,
  parseConnections,
  parseNote,
  scanConnections,
  splitConnectionLines,
} from "../src/markdown-parser.js";
import { bodyConnectionEdgeLines, readNoteText, stripConnectionsTo } from "../src/tools.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type LineCase = {
  name: string;
  line: string;
  expected: { type: string; target: string; context: string | null } | null;
};
type BodyEdge = { type: string; target: string; context: string | null };
type BodyCase = { name: string; body: string; edges: BodyEdge[]; problem: number | null };
type FileCase = { name: string; text: string; body: string; edges: BodyEdge[] };
type ParityFixture = { lines: LineCase[]; bodies: BodyCase[]; files: FileCase[] };

function loadFixture(): ParityFixture {
  const fixturePath = path.resolve(__dirname, "..", "..", "schema", "connection-line-parity.json");
  return JSON.parse(readFileSync(fixturePath, "utf-8")) as ParityFixture;
}

const fixture = loadFixture();

describe("connection-line regex parity (#338)", () => {
  test("fixture is present and non-trivial", () => {
    expect(fixture.lines.length).toBeGreaterThanOrEqual(15);
  });

  for (const c of fixture.lines) {
    test(c.name, () => {
      const m = c.line.match(CONNECTION_RE);
      if (c.expected === null) {
        expect(m).toBeNull();
      } else {
        expect(m).not.toBeNull();
        expect(m![1]).toBe(c.expected.type);
        expect(m![2]).toBe(c.expected.target);
        // Same quoted-context || em-dash-context || nothing combination
        // parseConnections uses (and Python's `or` chain mirrors).
        expect(m![3] || m![4] || null).toBe(c.expected.context);
      }
    });
  }
});

describe("connection-body splitter parity (#359)", () => {
  test("fixture is present and non-trivial", () => {
    expect(fixture.bodies.length).toBeGreaterThanOrEqual(15);
  });

  for (const c of fixture.bodies) {
    test(c.name, () => {
      const got = parseConnections(c.body).map((e) => ({
        type: e.type,
        target: e.target,
        context: e.context ?? null,
      }));
      expect(got).toEqual(c.edges);
      const scan = scanConnections(splitConnectionLines(normalizeNewlines(c.body)));
      expect(scan.problem === null ? null : scan.problem.index).toBe(c.problem);
    });
  }

  // The write-time validator and the delete cascade read the section with
  // the same rule (exact heading, fenced code skipped) as parseConnections and
  // ingest, so each body case must give them the same edges.
  for (const c of fixture.bodies) {
    test(`validator sees the same edges: ${c.name}`, () => {
      const got = [...bodyConnectionEdgeLines(c.body)].map(({ line }) => {
        const m = line.match(CONNECTION_RE)!;
        return { type: m[1], target: m[2], context: m[3] || m[4] || null };
      });
      expect(got).toEqual(c.edges);
    });
  }

  for (const c of fixture.bodies.filter((b) => b.edges.length === 0)) {
    test(`delete cascade strips nothing: ${c.name}`, () => {
      const targets = [...splitLinesForTargets(c.body)];
      expect(stripConnectionsTo(c.body, targets).removed).toBe(0);
    });
  }

  test("splitLinesLikePython is linear on a 1M-char body", async () => {
    const { splitLinesLikePython } = await import("../src/markdown-parser.js");
    const big = "x".repeat(1_000_000);
    const t0 = performance.now();
    expect(splitLinesLikePython(big)).toEqual([big]); // no boundaries → one line
    expect(performance.now() - t0).toBeLessThan(2000); // linear is a few ms
  });
});

/** Every target any line of `body` would carry if it were an edge. Bracket
 * references are left out: the cascade matches by target and never sees one,
 * since a note id never starts with "[". */
function* splitLinesForTargets(body: string): Generator<string> {
  for (const raw of body.split(/\r\n|[\n\r\v\f\u001c-\u001e\u0085\u2028\u2029]/)) {
    const m = raw.trim().match(CONNECTION_RE);
    if (m && !m[2].startsWith("[")) yield m[2];
  }
}

describe("delete cascade leaves fenced lines alone", () => {
  test("strips the real edge and keeps the same line inside a fence", () => {
    const body = [
      "## Connections",
      "- extends: note-a",
      "```",
      "- extends: note-a",
      "## Notes",
      "```",
      "- related: note-b",
      "",
    ].join("\n");
    const { content, removed } = stripConnectionsTo(body, ["note-a"]);
    expect(removed).toBe(1);
    expect(content).toBe(
      ["## Connections", "```", "- extends: note-a", "## Notes", "```", "- related: note-b", ""].join("\n"),
    );
  });
});

describe("connection whole-file parity", () => {
  // The same cases run through a real ingest in cli/tests/test_ingest.py:
  // both sides read the raw body after the frontmatter's closing line.
  test("fixture is present and non-trivial", () => {
    expect(fixture.files.length).toBeGreaterThanOrEqual(6);
  });

  // The vocabulary validator sees exactly the edges ingest will index.
  for (const c of fixture.files) {
    test(`validator sees ingest's edges: ${c.name}`, () => {
      const got = [...bodyConnectionEdgeLines(connectionBody(c.text))].map(({ line }) => {
        const m = line.match(CONNECTION_RE)!;
        return { type: m[1], target: m[2], context: m[3] || m[4] || null };
      });
      expect(got).toEqual(c.edges);
    });
  }

  // File I/O parity: the note's bytes on disk, read the way the server reads
  // notes (readNoteText), give the body ingest stores in docs.body (the
  // text the viewer renders) and the edges ingest indexes.
  // cli/tests/test_ingest.py runs the same files through a real ingest.
  for (const c of fixture.files) {
    test(`read from disk: ${c.name}`, async () => {
      const dir = mkdtempSync(path.join(os.tmpdir(), "schist-file-parity-"));
      try {
        const file = path.join(dir, "note.md");
        writeFileSync(file, Buffer.from(c.text, "utf-8"));
        const text = await readNoteText(file);
        expect(connectionBody(text)).toBe(c.body);
        expect(parseConnections(connectionBody(text)).map((e) => e.target)).toEqual(c.edges.map((e) => e.target));
      } finally {
        rmSync(dir, { recursive: true, force: true });
      }
    });
  }

  for (const c of fixture.files) {
    test(c.name, () => {
      let parsed: ReturnType<typeof parseNote>;
      try {
        parsed = parseNote(c.text);
      } catch {
        // gray-matter cannot read this note's YAML (e.g. never closed); the
        // connection slice must still match ingest.
        expect(parseConnections(connectionBody(c.text)).map((e) => e.target)).toEqual(c.edges.map((e) => e.target));
        return;
      }
      const got = parsed.connections.map((e) => ({
        type: e.type,
        target: e.target,
        context: e.context ?? null,
      }));
      expect(got).toEqual(c.edges);
    });
  }
});

describe("delete cascade on whole files", () => {
  test("a fence line in a frontmatter block scalar does not hide the edge", () => {
    const text = "---\ntitle: t\nsummary: |\n  ```\n---\n## Connections\n- extends: a\n- extends: b\n";
    const { content, removed } = stripConnectionsTo(text, ["a"]);
    expect(removed).toBe(1);
    expect(content).toBe("---\ntitle: t\nsummary: |\n  ```\n---\n## Connections\n- extends: b\n");
  });

  test("a heading in a frontmatter block scalar is not a section", () => {
    const text = "---\ntitle: t\nsummary: |\n  ## Connections\n  - extends: a\n---\nBody\n";
    expect(stripConnectionsTo(text, ["a"]).removed).toBe(0);
  });

  test("U+FEFF before ## does not end the section, as for the reader", () => {
    const text = "---\ntitle: t\n---\n## Connections\n- extends: a\n\ufeff## P\n- extends: b\n";
    expect(parseNote(text).connections.map((e) => e.target)).toEqual(["a", "b"]);
    const { content, removed } = stripConnectionsTo(text, ["b"]);
    expect(removed).toBe(1);
    expect(content).toBe("---\ntitle: t\n---\n## Connections\n- extends: a\n\ufeff## P\n");
  });

  test("a note the rule indexes nothing from is left alone", () => {
    const text = "---\ntitle: t\n---\n\n## Connections\n- extends: a\n<div>\n- extends: a\n\n- extends: b\n";
    expect(parseNote(text).connections).toEqual([]);
    const { content, removed } = stripConnectionsTo(text, ["a"]);
    expect(removed).toBe(0);
    expect(content).toBe(text);
  });
});
