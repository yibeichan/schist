/**
 * schema/index-contract.json is the single source for the vault-index table
 * contract (#130 D3) — tables, required tables/columns, rebuild survivors,
 * and the schema version ingest stamps into `PRAGMA user_version`. It is
 * consumed here (sqlite-reader.ts) and by cli/schist/index_contract.py; the
 * per-language REQUIRED_TABLES constants it replaced had drifted (#339: the
 * TS mirror omitted `docs`).
 *
 * Two layers of pinning make the single source real:
 *   1. mirror drift — the baked-in fallback each component ships (repo-root
 *      schema/ files ship with neither package) must equal the JSON;
 *   2. schema.sql parity — the contract must describe what schema.sql
 *      actually creates, so neither can change without the other.
 * The Python twin lives in cli/tests/test_index_contract.py.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import Database from "better-sqlite3";
import {
  INDEX_CONTRACT_FALLBACK,
  INDEX_SCHEMA_VERSION,
  loadIndexContract,
} from "../src/sqlite-reader.js";
import type { IndexContract } from "../src/sqlite-reader.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "..", "..");

const contract = JSON.parse(
  readFileSync(path.join(repoRoot, "schema", "index-contract.json"), "utf-8"),
) as IndexContract;
const schemaSql = readFileSync(
  path.join(repoRoot, "cli", "schist", "schema.sql"),
  "utf-8",
);

describe("index-contract mirror drift", () => {
  test("baked-in fallback mirrors schema/index-contract.json", () => {
    expect(INDEX_CONTRACT_FALLBACK).toEqual(contract);
  });

  test("loadIndexContract() returns the canonical contract in a repo checkout", () => {
    expect(loadIndexContract()).toEqual(contract);
  });

  test("INDEX_SCHEMA_VERSION is the contract's schemaVersion", () => {
    expect(INDEX_SCHEMA_VERSION).toBe(contract.schemaVersion);
  });
});

describe("index-contract ↔ schema.sql parity", () => {
  function materializeSchema(): { tables: Set<string>; docsColumns: string[] } {
    const db = new Database(":memory:");
    try {
      db.exec(schemaSql);
      const rows = db
        .prepare("SELECT name FROM sqlite_master WHERE type = 'table'")
        .all() as Array<{ name: string }>;
      // docs_fts_* are FTS5 shadow tables and sqlite_sequence is the
      // AUTOINCREMENT bookkeeping table — implementation details, not part
      // of the contract surface.
      const tables = new Set(
        rows
          .map((r) => r.name)
          .filter((n) => n !== "sqlite_sequence" && !n.startsWith("docs_fts_")),
      );
      const docsColumns = (db.pragma("table_info(docs)") as Array<{ name: string }>).map(
        (c) => c.name,
      );
      return { tables, docsColumns };
    } finally {
      db.close();
    }
  }

  test("schemaVersion is a positive integer", () => {
    expect(Number.isInteger(contract.schemaVersion)).toBe(true);
    expect(contract.schemaVersion).toBeGreaterThan(0);
  });

  test("contract `tables` is exactly what schema.sql creates", () => {
    const { tables } = materializeSchema();
    expect(new Set(contract.tables)).toEqual(tables);
  });

  test("requiredTables ⊆ tables", () => {
    for (const t of contract.requiredTables) {
      expect(contract.tables).toContain(t);
    }
  });

  test("requiredTables includes docs (#339: a DB without docs is unusable)", () => {
    expect(contract.requiredTables).toContain("docs");
  });

  test("requiredDocsColumns ⊆ the docs columns schema.sql creates", () => {
    const { docsColumns } = materializeSchema();
    for (const c of contract.requiredDocsColumns) {
      expect(docsColumns).toContain(c);
    }
  });

  test("rebuildSurvivors match schema.sql's DROP/CREATE structure", () => {
    // A survivor must be created with IF NOT EXISTS and must NOT appear in
    // the DROP list; every other contract table must be dropped, or a
    // commit-path rebuild (ingest against the existing DB) would silently
    // keep its stale rows.
    const dropped = new Set(
      [...schemaSql.matchAll(/DROP TABLE IF EXISTS (\w+)/gi)].map((m) => m[1]),
    );
    const ifNotExists = new Set(
      [...schemaSql.matchAll(/CREATE (?:VIRTUAL )?TABLE IF NOT EXISTS (\w+)/gi)].map(
        (m) => m[1],
      ),
    );

    for (const survivor of contract.rebuildSurvivors) {
      expect(contract.tables).toContain(survivor);
      expect(ifNotExists.has(survivor)).toBe(true);
      expect(dropped.has(survivor)).toBe(false);
    }
    for (const table of contract.tables) {
      if (!contract.rebuildSurvivors.includes(table)) {
        expect(dropped.has(table)).toBe(true);
      }
    }
  });
});
