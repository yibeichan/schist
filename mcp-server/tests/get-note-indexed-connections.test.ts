// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// sqlite-reader getNote parses connections from docs.body, which ingest
// stores stripped; the stripped text can read as a section the raw body
// (where ingest takes edges from) does not have. getNote shows only the
// connections the edges table holds.

import { getNote } from "../src/sqlite-reader.js";
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import Database from "better-sqlite3";

let dir = "";

beforeAll(async () => {
  dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-getnote-edges-"));
  await fs.mkdir(path.join(dir, ".schist"), { recursive: true });
  const db = new Database(path.join(dir, ".schist", "schist.db"));
  db.exec(`
    CREATE TABLE docs (
      id TEXT PRIMARY KEY, title TEXT NOT NULL, date TEXT, status TEXT,
      tags TEXT, concepts TEXT, body TEXT NOT NULL, scope TEXT, source TEXT,
      confidence TEXT, file_ref TEXT
    );
    CREATE TABLE edges (
      id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
      target TEXT NOT NULL, type TEXT NOT NULL, context TEXT,
      UNIQUE(source, target, type)
    );
    INSERT INTO docs (id, title, body, tags) VALUES
      ('notes/indented.md', 'Indented', '## Connections\n- extends: notes/fake.md', '[]'),
      ('notes/real.md', 'Real', '## Connections\n- extends: notes/a.md\n- supports: notes/b.md', '[]');
    INSERT INTO edges (source, target, type) VALUES
      ('notes/real.md', 'notes/a.md', 'extends'),
      ('notes/real.md', 'notes/b.md', 'supports');
  `);
  db.close();
});

afterAll(async () => {
  await fs.rm(dir, { recursive: true, force: true });
});

test("a connection the index does not hold is not shown", () => {
  // Stored stripped: the raw body opened with "    ## Connections", code.
  expect(getNote(dir, "notes/indented.md")!.connections).toEqual([]);
});

test("indexed connections are shown", () => {
  expect(getNote(dir, "notes/real.md")!.connections.map((c) => c.target)).toEqual(["notes/a.md", "notes/b.md"]);
});
