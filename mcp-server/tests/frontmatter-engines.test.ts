// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { parseNote } from "../src/markdown-parser.js";

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
});
