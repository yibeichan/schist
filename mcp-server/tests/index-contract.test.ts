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
import { jest } from "@jest/globals";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import * as os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
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

describe("schemaSqlDigest pins the materialized DDL", () => {
  test("recomputed digest matches the contract", () => {
    // ANY schema.sql DDL edit must force a visible contract diff, even one
    // that dodges every list-based check (e.g. adding a docs column + a
    // reader SELECT while forgetting the schemaVersion bump and
    // requiredDocsColumns entry — requiredDocsColumns is only checked as a
    // subset). Recompute recipe documented in cli/schist/index_contract.py;
    // the Python twin recomputes it identically. On failure: update
    // schema/index-contract.json + both baked mirrors AND decide whether
    // schemaVersion must bump.
    const db = new Database(":memory:");
    let computed: string;
    try {
      db.exec(schemaSql);
      const rows = db
        .prepare("SELECT type, name, sql FROM sqlite_master")
        .all() as Array<{ type: string; name: string; sql: string | null }>;
      const kept = rows
        .filter((r) => !r.name.startsWith("sqlite_") && !r.name.startsWith("docs_fts_"))
        .map((r) => `${r.type}\x1f${r.name}\x1f${r.sql ?? ""}`)
        .sort();
      computed = createHash("sha256").update(kept.join("\x1e"), "utf-8").digest("hex");
    } finally {
      db.close();
    }
    expect(contract.schemaSqlDigest).toBe(computed);
  });
});

describe("loadIndexContract fallback paths (the only paths production npm installs exercise)", () => {
  let tmpDir: string;
  let warnSpy: jest.SpiedFunction<typeof console.warn>;

  beforeEach(() => {
    tmpDir = mkdtempSync(path.join(os.tmpdir(), "schist-contract-"));
    warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
  });
  afterEach(() => {
    warnSpy.mockRestore();
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test("missing file falls back SILENTLY — absence is the normal published-package state", () => {
    const result = loadIndexContract(path.join(tmpDir, "does-not-exist.json"));
    expect(result).toEqual(INDEX_CONTRACT_FALLBACK);
    expect(warnSpy).not.toHaveBeenCalled();
  });

  test("malformed JSON warns and falls back", () => {
    const bad = path.join(tmpDir, "index-contract.json");
    writeFileSync(bad, "{ not json");
    const result = loadIndexContract(bad);
    expect(result).toEqual(INDEX_CONTRACT_FALLBACK);
    expect(warnSpy).toHaveBeenCalledTimes(1);
  });

  test("invalid shape warns and falls back", () => {
    const bad = path.join(tmpDir, "index-contract.json");
    writeFileSync(bad, JSON.stringify({ ...INDEX_CONTRACT_FALLBACK, requiredTables: [] }));
    const result = loadIndexContract(bad);
    expect(result).toEqual(INDEX_CONTRACT_FALLBACK);
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(String(warnSpy.mock.calls[0][0])).toContain("malformed");
  });

  test("integral-float schemaVersion (`2.0`) is the integer 2 — accepted", () => {
    // JSON has one number type: JS cannot even observe the difference, and
    // the Python loader coerces to match. A hand-edited `2.0` must not
    // desync the two languages.
    const f = path.join(tmpDir, "index-contract.json");
    writeFileSync(f, JSON.stringify({ ...INDEX_CONTRACT_FALLBACK, schemaVersion: 2.0 }));
    const result = loadIndexContract(f);
    expect(result.schemaVersion).toBe(2);
    expect(warnSpy).not.toHaveBeenCalled();
  });

  test("schemaVersion beyond 32 bits is rejected — user_version is a signed 32-bit field", () => {
    const f = path.join(tmpDir, "index-contract.json");
    writeFileSync(f, JSON.stringify({ ...INDEX_CONTRACT_FALLBACK, schemaVersion: 2 ** 31 }));
    const result = loadIndexContract(f);
    expect(result).toEqual(INDEX_CONTRACT_FALLBACK);
    expect(warnSpy).toHaveBeenCalledTimes(1);
  });

  test("non-hex schemaSqlDigest is rejected", () => {
    const f = path.join(tmpDir, "index-contract.json");
    writeFileSync(f, JSON.stringify({ ...INDEX_CONTRACT_FALLBACK, schemaSqlDigest: "zz" }));
    const result = loadIndexContract(f);
    expect(result).toEqual(INDEX_CONTRACT_FALLBACK);
    expect(warnSpy).toHaveBeenCalledTimes(1);
  });
});
