// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  buildNote, FrontmatterSplitError, frontmatterLength, frontmatterSplitMismatch, hasNonYamlOpener, parseNote,
  parseNoteForRewrite,
} from "../src/markdown-parser.js";

// gray-matter reads the text after the opening `---` as a language name and
// ships a `javascript` engine that evaluates the block. Note content comes
// from every participant through the hub, so only YAML may be parsed.
describe("frontmatter languages", () => {
  let dir: string;
  let marker: string;

  beforeEach(() => {
    dir = mkdtempSync(path.join(tmpdir(), "schist-fm-"));
    marker = path.join(dir, "ran");
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  const payload = (opener: string) =>
    `${opener}\n{ title: (require('fs').writeFileSync(${JSON.stringify(marker)}, 'x'), 'x') }\n---\nbody\n`;

  test.each(["---js", "---javascript", "--- js", "---JS", "﻿---js", "---coffee", "---cson"])(
    "%j is refused without being evaluated",
    (opener) => {
      expect(() => parseNote(payload(opener))).toThrow(/frontmatter/i);
      expect(existsSync(marker)).toBe(false);
    },
  );

  test("JSON frontmatter is refused too: notes are YAML", () => {
    expect(() => parseNote('---json\n{"title": "x"}\n---\nbody\n')).toThrow(/frontmatter/i);
  });

  test.each(["---", "---yaml", "--- yml", "---   "])("%j still parses as YAML", (opener) => {
    const parsed = parseNote(`${opener}\ntitle: Plain\n---\nbody\n`);
    expect(parsed.metadata.title).toBe("Plain");
    expect(parsed.body).toBe("body\n");
  });

  test("a note without frontmatter is unaffected", () => {
    expect(parseNote("just text\n").body).toBe("just text\n");
  });

  // buildNote writes a note from metadata and a body. The body comes from
  // the note being rewritten (update_note, the concept cascade) or from the
  // caller (create_note), so it is note content, never parsed as one.
  test.each(["---js", "---javascript", "--- js", "---JS", "\uFEFF---js", "---coffee"])(
    "a body opening with %j is written as text, not evaluated",
    (opener) => {
      const body = payload(opener);
      const out = buildNote({ title: "t" }, body);
      expect(existsSync(marker)).toBe(false);
      expect(out).toBe(`---\ntitle: t\n---\n${body}`);
    },
  );

  test("a body opening with a YAML block is not merged into the frontmatter", () => {
    const body = "---\nstatus: final\nsource_agent: someone-else\n---\nbody\n";
    const out = buildNote({ title: "t", status: "draft" }, body);
    expect(out).toBe(`---\ntitle: t\nstatus: draft\n---\n${body}`);
  });

  test("an ordinary note is written as before", () => {
    expect(buildNote({ title: "t", tags: ["a"] }, "Body text\n")).toBe(
      "---\ntitle: t\ntags:\n  - a\n---\nBody text\n",
    );
  });

  test.each([
    "---yaml\ntitle: x\n---\nbody\n",
    "---\nsource_agent: someone-else\n---\nbody\n",
    "---js\n(1, {})\n---\nbody\n",
    "\uFEFF---\ntitle: x\n---\nbody\n",
    "\uFEFF\uFEFF---js\n(1, {})\n---\nbody\n",
    // Openers ingest (python-frontmatter, frontmatterLength) reads as
    // frontmatter and gray-matter does not.
    " \n---\nconcepts: [x]\n---\nhi\n",
    "\n\n---\ntitle: x\n---\nbody\n",
    '{\n"concepts": ["x"], "source_agent": "someone-else"\n}\nProse.\n',
    '{\n"concepts": ["x"]\n}\n## Connections\n\n- extends: notes/a.md\n',
    '+++\nconcepts = ["x"]\n+++\nbody\n',
    "-----\ntitle: T\nconcepts: [c]\n-----\nbody\n",
    "\t---\ntitle: x\n---\nbody\n",
  ])("with no metadata, a body opening with %j stays body text", (body) => {
    const out = buildNote({}, body);
    expect(hasNonYamlOpener(out)).toBe(false);
    const parsed = parseNote(out);
    expect(parsed.metadata).toEqual({});
    expect(parsed.body).toBe(body);
    // Ingest reads only the empty block, so the body is body text there too.
    expect(frontmatterLength(out)).toBe("---\n---\n".length);
    expect(frontmatterSplitMismatch(out)).toBeNull();
  });

  test.each([
    "{\n}\nbody\n",
    "+++\n+++\nbody\n",
  ])("with metadata, a body opening with %j stays body text", (body) => {
    const out = buildNote({ title: "t" }, body);
    expect(out).toBe(`---\ntitle: t\n---\n${body}`);
    expect(frontmatterLength(out)).toBe("---\ntitle: t\n---\n".length);
  });

  test("with no metadata, an ordinary body is written unchanged", () => {
    expect(buildNote({}, "just text\n")).toBe("just text\n");
  });
});

// Writers that rebuild a note from parseNote's metadata and body (update_note,
// the concept cascade) must read the frontmatter block ingest reads, or the
// rewrite changes what ingest indexes.
describe("frontmatter split agreement", () => {
  test.each([
    ["plain", "just text\n"],
    ["ordinary", "---\ntitle: t\n---\nbody\n"],
    ["blank line after the block", "---\ntitle: t\n---\n\nbody\n"],
    ["empty block", "---\n---\nbody\n"],
    ["closing line with trailing spaces", "---\ntitle: t\n---  \nbody\n"],
    ["opening line with trailing spaces", "---   \ntitle: t\n---\nbody\n"],
    ["closed at the end of the file", "---\ntitle: t\n---"],
    ["CRLF", "---\r\ntitle: t\r\n---\r\nbody\r\n"],
    ["flow hashtags", "---\ntags: [#a, #b]\n---\nbody\n"],
  ])("%s: same block", (_name, note) => {
    expect(frontmatterSplitMismatch(note)).toBeNull();
    expect(() => parseNoteForRewrite(note, "update_note")).not.toThrow();
  });

  test.each([
    ["five-dash fences", "-----\ntitle: T\nconcepts: [c]\n-----\nbody\n", /reads as body text/],
    ["leading blank line", "\n---\ntitle: T\n---\nbody\n", /reads as body text/],
    ["leading spaces", "  \n---\ntitle: T\n---\nbody\n", /reads as body text/],
    ["JSON block", '{\n"concepts": ["x"]\n}\nbody\n', /reads as body text/],
    ["TOML block", '+++\nconcepts = ["x"]\n+++\nbody\n', /reads as body text/],
    ["BOM", "\uFEFF---\ntitle: T\n---\nbody\n", /the index does not/],
    ["language opener", "---yaml\ntitle: T\n---\nbody\n", /the index does not/],
    ["never closed", "---\ntitle: T\n", /the index does not/],
    ["closes on a five-dash line", "---\ntitle: T\n-----\nconcepts: [c]\n---\nbody\n", /different lines/],
    ["closes on ---foo", "---\ntitle: T\n---foo\nconcepts: [c]\n---\nbody\n", /different lines/],
  ])("%s: refused", (_name, note, why) => {
    expect(frontmatterSplitMismatch(note)).toMatch(why);
    expect(() => parseNoteForRewrite(note, "update_note")).toThrow(FrontmatterSplitError);
    expect(() => parseNoteForRewrite(note, "update_note")).toThrow(/^update_note refused: .*Nothing was written\.$/);
  });
});
