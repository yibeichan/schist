import { describe, expect, it, beforeEach, afterEach } from "@jest/globals";
import * as os from "os";
import * as path from "path";
import { fileURLToPath } from "url";
import * as fs from "fs/promises";
import Database from "better-sqlite3";
import { list_domains } from "../src/tools.js";
import { resetCursorForTesting, issueCursor } from "../src/protocol/index.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// list_domains hits <vault>/.schist/schist.db (domains table). Tests build a
// minimal schema and seed N rows so the cursor-pipeline behaviours can be
// exercised without a real ingest run.

async function makeVault(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-ld-tool-test-"));
  const dbDir = path.join(dir, ".schist");
  await fs.mkdir(dbDir, { recursive: true });
  const dbPath = path.join(dbDir, "schist.db");

  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE domains (
      slug        TEXT PRIMARY KEY,
      label       TEXT NOT NULL,
      description TEXT,
      parent_slug TEXT REFERENCES domains(slug)
    );
  `);
  db.close();
  return dir;
}

// Seed N domains. Slugs are zero-padded so the ORDER BY parent_slug NULLS
// FIRST, slug pagination is deterministic and lexicographic.
async function seed(
  vaultRoot: string,
  n: number,
  opts?: { parent_slug?: string; slugPrefix?: string },
): Promise<void> {
  const db = new Database(path.join(vaultRoot, ".schist", "schist.db"));
  try {
    const stmt = db.prepare(
      `INSERT INTO domains (slug, label, description, parent_slug) VALUES (?, ?, ?, ?)`,
    );
    const prefix = opts?.slugPrefix ?? "domain";
    for (let i = 0; i < n; i++) {
      const idx = String(i).padStart(3, "0");
      stmt.run(
        `${prefix}-${idx}`,
        `Domain ${idx}`,
        `description ${idx}`,
        opts?.parent_slug ?? null,
      );
    }
  } finally {
    db.close();
  }
}

let vaultRoot: string;
const envSnapshot: Record<string, string | undefined> = {};
const envKeys = ["SCHIST_AGENT_ID", "SCHIST_AGENT_NAME", "SCHIST_ALLOWED_AGENTS"] as const;

beforeEach(async () => {
  for (const k of envKeys) {
    envSnapshot[k] = process.env[k];
    delete process.env[k];
  }
  vaultRoot = await makeVault();
  resetCursorForTesting();
});

afterEach(async () => {
  for (const k of envKeys) {
    if (envSnapshot[k] === undefined) delete process.env[k];
    else process.env[k] = envSnapshot[k];
  }
  await fs.rm(vaultRoot, { recursive: true, force: true });
});

// ── canonicalize errors ────────────────────────────────────────────────────

describe("list_domains tool — canonicalize errors", () => {
  it("returns INVALID_ARG when an arg is unhashable (NaN)", async () => {
    const r = await list_domains(vaultRoot, { limit: NaN } as never);
    expect(r).toEqual({ error: "INVALID_ARG", message: expect.stringContaining("non-finite") });
  });
});

// ── cursor decoding ────────────────────────────────────────────────────────

describe("list_domains tool — cursor decoding", () => {
  it("returns CURSOR_INVALID_SIGNATURE when the cursor signature is malformed", async () => {
    await seed(vaultRoot, 5);
    const r = await list_domains(vaultRoot, { cursor: "garbage.notreallya.cursor" });
    expect(r).toEqual({ error: "CURSOR_INVALID_SIGNATURE", message: expect.any(String) });
  });

  it("returns CURSOR_WRONG_TOOL when a cursor for a different tool is presented", async () => {
    await seed(vaultRoot, 5);
    const c = issueCursor({ tool: "list_concepts", queryHash: "deadbeef", offset: 5 });
    const r = await list_domains(vaultRoot, { cursor: c });
    expect(r).toEqual({ error: "CURSOR_WRONG_TOOL", message: expect.stringContaining("list_concepts") });
  });

  it("returns CURSOR_INVALID_SIGNATURE when cursor queryHash does NOT match current args (binding policy)", async () => {
    await seed(vaultRoot, 5);
    const c = issueCursor({ tool: "list_domains", queryHash: "0".repeat(64), offset: 2 });
    const r = await list_domains(vaultRoot, { cursor: c });
    expect(r).toEqual({
      error: "CURSOR_INVALID_SIGNATURE",
      message: expect.stringContaining("different query"),
    });
  });

  it("accepts a cursor whose encoded queryHash matches the current args (round-trip)", async () => {
    await seed(vaultRoot, 10);
    const { canonicalizeQueryHash } = await import("../src/protocol/index.js");
    const args = { limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    expect(ch.ok).toBe(true);
    if (!ch.ok) return;
    const c = issueCursor({ tool: "list_domains", queryHash: ch.queryHash, offset: 3 });
    const r = await list_domains(vaultRoot, { ...args, cursor: c });
    expect(r).toHaveProperty("domains");
    expect(r).not.toHaveProperty("error");
    if (!("domains" in r)) throw new Error("expected domains");
    // Page 2 starting at offset 3 in a 10-domain vault should return up to
    // limit (3) rows, beginning with the 4th domain lexicographically.
    expect(r.domains.length).toBe(3);
    expect(r.domains[0].slug).toBe("domain-003");
  });
});

// ── identical-query refusal ────────────────────────────────────────────────

describe("list_domains tool — identical-query refusal", () => {
  it("returns CURSOR_REQUIRED on identical (tool, queryHash, activeOwner) within TTL", async () => {
    await seed(vaultRoot, 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    const args = { limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "list_domains", queryHash: ch.queryHash, owner: "", verboseEnabled: false });
    const r = await list_domains(vaultRoot, args);
    expect(r).toEqual({
      error: "CURSOR_REQUIRED",
      message: expect.stringContaining("Identical query"),
    });
  });

  it("does NOT refuse when activeOwner differs (per-call owner namespace)", async () => {
    await seed(vaultRoot, 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    // Record under owner "yibei". Current call's activeOwner resolves from
    // SCHIST_AGENT_NAME → SCHIST_AGENT_ID → "", both env vars are unset in
    // beforeEach, so it becomes "" — DIFFERENT from the recorded "yibei".
    const args = { limit: 3 };
    const ch = canonicalizeQueryHash(args, "yibei");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "list_domains", queryHash: ch.queryHash, owner: "yibei", verboseEnabled: false });
    const r = await list_domains(vaultRoot, args);
    expect(r).toHaveProperty("domains");
    expect(r).not.toHaveProperty("error");
  });
});

// ── pagination + cursor issuance ───────────────────────────────────────────

describe("list_domains tool — pagination + cursor issuance", () => {
  it("returns a cursor + first 100 domains on default limit when seeded with 120", async () => {
    await seed(vaultRoot, 120);
    const r = await list_domains(vaultRoot, {});
    if (!("domains" in r)) throw new Error("expected domains");
    expect(r.domains.length).toBe(100);
    expect(typeof r.cursor).toBe("string");
  });

  it("cursor advances pagination — page 2 returns the remaining 20, no overlap", async () => {
    await seed(vaultRoot, 120);
    const r1 = await list_domains(vaultRoot, {});
    if (!("domains" in r1) || !r1.cursor) throw new Error("expected page-1 cursor");
    const r2 = await list_domains(vaultRoot, { cursor: r1.cursor });
    if (!("domains" in r2)) throw new Error("expected domains");
    expect(r2.domains.length).toBe(20);
    expect(r2.cursor).toBeUndefined();
    const slugs1 = new Set(r1.domains.map(d => d.slug));
    for (const d of r2.domains) {
      expect(slugs1.has(d.slug)).toBe(false);
    }
  });

  it("the last page does NOT return a cursor", async () => {
    await seed(vaultRoot, 8);
    const r1 = await list_domains(vaultRoot, { limit: 5 });
    if (!("domains" in r1) || !r1.cursor) throw new Error("expected page-1 cursor");
    const r2 = await list_domains(vaultRoot, { limit: 5, cursor: r1.cursor });
    if (!("domains" in r2)) throw new Error("expected domains");
    expect(r2.domains.length).toBe(3);
    expect(r2.cursor).toBeUndefined();
  });

  it("clamps limit at 500 (cap from spec)", async () => {
    await seed(vaultRoot, 600);
    const r = await list_domains(vaultRoot, { limit: 9999 });
    if (!("domains" in r)) throw new Error("expected domains");
    expect(r.domains.length).toBeLessThanOrEqual(500);
  });

  it("collapses limit: 0 to default 100", async () => {
    await seed(vaultRoot, 120);
    const r = await list_domains(vaultRoot, { limit: 0 });
    if (!("domains" in r)) throw new Error("expected domains");
    expect(r.domains.length).toBe(100);
    expect(r.cursor).toBeDefined();
  });

  it("collapses negative limit to default 100", async () => {
    await seed(vaultRoot, 120);
    const r = await list_domains(vaultRoot, { limit: -1 });
    if (!("domains" in r)) throw new Error("expected domains");
    expect(r.domains.length).toBe(100);
    expect(r.cursor).toBeDefined();
  });

  it("returns empty domains + no cursor when no rows match", async () => {
    // Empty vault — no seed call.
    const r = await list_domains(vaultRoot, {});
    if (!("domains" in r)) throw new Error("expected domains");
    expect(r.domains).toEqual([]);
    expect(r.cursor).toBeUndefined();
  });
});

// ── tiebreaker stability ───────────────────────────────────────────────────

describe("list_domains tool — parent_slug NULLS FIRST, slug ordering stability", () => {
  // ORDER BY parent_slug NULLS FIRST, slug. Seeded with 10 parent (null
  // parent_slug) + 10 child (parent_slug = "parent-000") domains. The
  // pagination across 4 pages of 5 must visit each row exactly once and
  // follow the spec order: NULLs first by slug, then non-NULLs by slug.
  it("paginates 10 parent + 10 child domains across 4 pages of limit=5 (no duplicates, deterministic order)", async () => {
    await seed(vaultRoot, 10, { slugPrefix: "parent" });
    await seed(vaultRoot, 10, { slugPrefix: "child", parent_slug: "parent-000" });

    const seen: string[] = [];
    let cursor: string | undefined;
    let page = 0;
    while (page < 10) {
      const r = await list_domains(vaultRoot, { limit: 5, cursor });
      if (!("domains" in r)) throw new Error(`unexpected error on page ${page}: ${JSON.stringify(r)}`);
      for (const d of r.domains) {
        expect(seen.includes(d.slug)).toBe(false);
        seen.push(d.slug);
      }
      cursor = r.cursor;
      page++;
      if (cursor === undefined) break;
    }
    expect(seen.length).toBe(20);
    // NULLS FIRST: all 10 parent-* (parent_slug NULL) come first in slug ASC,
    // then 10 child-* (parent_slug = "parent-000") in slug ASC.
    const expected = [
      ...Array.from({ length: 10 }, (_, i) => `parent-${String(i).padStart(3, "0")}`),
      ...Array.from({ length: 10 }, (_, i) => `child-${String(i).padStart(3, "0")}`),
    ];
    expect(seen).toEqual(expected);
  });
});

// ── empty owner ────────────────────────────────────────────────────────────

describe("list_domains tool — empty owner", () => {
  it("returns a valid response when both SCHIST_AGENT_NAME and SCHIST_AGENT_ID are unset (owner='')", async () => {
    // beforeEach already deletes both env vars. Reassert here for clarity.
    delete process.env.SCHIST_AGENT_NAME;
    delete process.env.SCHIST_AGENT_ID;
    await seed(vaultRoot, 3);
    const r = await list_domains(vaultRoot, {});
    if (!("domains" in r)) throw new Error("expected domains");
    expect(r.domains.length).toBe(3);
    expect(r.cursor).toBeUndefined();
  });
});

// ── normalizeError fallthrough ─────────────────────────────────────────────

describe("list_domains tool — normalizeError fallthrough wiring", () => {
  it("handler body contains try/catch wrapping sqliteReader.listDomains with INGEST_ERROR fallback", async () => {
    // sqlite-reader.listDomains currently swallows DB errors and returns [],
    // so we cannot trigger the handler's catch via DROP TABLE / corrupt DB.
    // ESM read-only-module rules also prevent jest.spyOn on the namespace
    // import. Verify the defensive try/catch + normalizeError("INGEST_ERROR")
    // wiring via source inspection — same approach as the JSDoc smoke test.
    const src = await fs.readFile(
      path.join(__dirname, "..", "src", "tools.ts"),
      "utf-8",
    );
    const fnIdx = src.indexOf("export async function list_domains(");
    expect(fnIdx).toBeGreaterThan(0);
    // The handler body spans ~70 lines after the function header.
    const body = src.slice(fnIdx, fnIdx + 3500);
    expect(body).toMatch(/sqliteReader\.listDomains\(/);
    expect(body).toMatch(/catch\s*\(e:\s*unknown\)/);
    expect(body).toMatch(/normalizeError\(e,\s*["']INGEST_ERROR["']\)/);
  });
});

// ── response shape ─────────────────────────────────────────────────────────

describe("list_domains tool — response shape", () => {
  it("returns { domains: Domain[] } (not a bare array)", async () => {
    await seed(vaultRoot, 2);
    const r = await list_domains(vaultRoot, {});
    expect(Array.isArray(r)).toBe(false);
    if (!("domains" in r)) throw new Error("expected domains");
    expect(Array.isArray(r.domains)).toBe(true);
    // Domain shape: slug, label, description?, parent_slug?
    for (const d of r.domains) {
      expect(typeof d.slug).toBe("string");
      expect(typeof d.label).toBe("string");
    }
  });
});

// ── JSDoc smoke ────────────────────────────────────────────────────────────

describe("list_domains tool — JSDoc smoke", () => {
  it("source file contains the handler's spec doc reference", async () => {
    const src = await fs.readFile(
      path.join(__dirname, "..", "src", "tools.ts"),
      "utf-8",
    );
    const fnIdx = src.indexOf("export async function list_domains(");
    expect(fnIdx).toBeGreaterThan(0);
    // Look backwards ~1500 chars for the JSDoc.
    const head = src.slice(Math.max(0, fnIdx - 1500), fnIdx);
    expect(head).toMatch(/list_domains tool handler/);
    expect(head).toMatch(/Spec:\s*docs\/superpowers\/specs\//);
  });
});
