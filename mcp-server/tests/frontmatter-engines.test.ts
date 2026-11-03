// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { buildNote, hasNonYamlOpener, parseNote } from "../src/markdown-parser.js";

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
  ])("with no metadata, a body opening with %j stays body text", (body) => {
    const out = buildNote({}, body);
    expect(hasNonYamlOpener(out)).toBe(false);
    const parsed = parseNote(out);
    expect(parsed.metadata).toEqual({});
    expect(parsed.body).toBe(body);
  });

  test("with no metadata, an ordinary body is written unchanged", () => {
    expect(buildNote({}, "just text\n")).toBe("just text\n");
  });
});
