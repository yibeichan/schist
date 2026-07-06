/**
 * #130 D3 — index schema version in `PRAGMA user_version`.
 *
 * Ingest stamps `user_version = INDEX_SCHEMA_VERSION` atomically with the
 * data commit (0 while in flight). A reader that finds a non-zero version
 * other than its own must treat the DB as stale and force a rebuild —
 * the index is disposable, so rebuild IS the migration path. The stale path
 * must go through ensureSchemaCurrent's rebuild-once → recheck → typed-error
 * pattern: a newer mcp-server paired with an older installed schist-ingest
 * still stamps the OLD version after a rebuild, and the recheck is what
 * turns that loop into an actionable "upgrade schist-ingest" error.
 *
 * Also pins the #339 fix: `docs` is now in the required-tables set, so a DB
 * missing it rebuilds instead of failing on the first docs query.
 */
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import { fileURLToPath } from "url";
import Database from "better-sqlite3";
import * as sqliteReader from "../src/sqlite-reader.js";
import { INDEX_SCHEMA_VERSION, resetSchemaCacheForTesting } from "../src/sqlite-reader.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "..", "..");

const createdDirs = new Set<string>();
const envSnapshot: Record<string, string | undefined> = {};

async function makeBinDir(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-ingest-bin-"));
  createdDirs.add(dir);
  return dir;
}

/** Point SCHIST_INGEST_BIN at the repo's real Python ingest. */
async function useLocalSchistIngestBin(): Promise<void> {
  const dir = await makeBinDir();
  const bin = path.join(dir, "schist-ingest-local");
  const python = path.join(repoRoot, "cli", ".venv", "bin", "python");
  const cliDir = path.join(repoRoot, "cli");
  await fs.writeFile(
    bin,
    [
      "#!/bin/sh",
      `if [ -x ${JSON.stringify(python)} ]; then`,
      `  PYTHONPATH=${JSON.stringify(cliDir)} exec ${JSON.stringify(python)} -m schist.ingest "$@"`,
      "fi",
      `cd ${JSON.stringify(cliDir)} && exec uv run --with . python -m schist.ingest "$@"`,
      "",
    ].join("\n"),
    { mode: 0o755 },
  );
  process.env.SCHIST_INGEST_BIN = bin;
}

/**
 * Point SCHIST_INGEST_BIN at a fake that logs each invocation and exits 0
 * WITHOUT touching the DB — the observable behavior of an installed
 * schist-ingest so old it reproduces the same stale schema/version.
 */
async function useNoopSchistIngestBin(): Promise<string> {
  const dir = await makeBinDir();
  const bin = path.join(dir, "schist-ingest-noop");
  const log = path.join(dir, "calls.log");
  await fs.writeFile(bin, `#!/bin/sh\necho called >> ${JSON.stringify(log)}\nexit 0\n`, {
    mode: 0o755,
  });
  process.env.SCHIST_INGEST_BIN = bin;
  return log;
}

/** Point SCHIST_INGEST_BIN at a fake that fails loudly if ever invoked. */
async function useFailLoudSchistIngestBin(): Promise<void> {
  const dir = await makeBinDir();
  const bin = path.join(dir, "schist-ingest-failloud");
  await fs.writeFile(
    bin,
    '#!/bin/sh\necho "unexpected schist-ingest invocation" >&2\nexit 1\n',
    { mode: 0o755 },
  );
  process.env.SCHIST_INGEST_BIN = bin;
}

/** Real vault (schist.yaml present, so staleness detection engages) with one note. */
async function makeVault(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-version-"));
  createdDirs.add(dir);
  await fs.writeFile(
    path.join(dir, "schist.yaml"),
    ["name: Version Test", "write_branch: drafts", "directories:", "  - notes", ""].join("\n"),
  );
  await fs.mkdir(path.join(dir, "notes"), { recursive: true });
  await fs.writeFile(
    path.join(dir, "notes", "2026-07-06-version.md"),
    "---\ntitle: Version Test\ndate: 2026-07-06\n---\n\nhaystack body.\n",
  );
  return dir;
}

/**
 * Hand-build a schist.db from the CURRENT schema.sql (so table/column checks
 * pass and only user_version varies), seeded with one doc the FTS trigger
 * indexes, stamped with the given user_version.
 */
async function buildDbAtVersion(vault: string, userVersion: number): Promise<string> {
  const schemaSql = await fs.readFile(path.join(repoRoot, "cli", "schist", "schema.sql"), "utf-8");
  const dbDir = path.join(vault, ".schist");
  await fs.mkdir(dbDir, { recursive: true });
  const dbPath = path.join(dbDir, "schist.db");
  const db = new Database(dbPath);
  db.exec(schemaSql);
  db.prepare(
    "INSERT INTO docs (id, title, body, scope) VALUES (?, ?, ?, 'global')",
  ).run("notes/hand-built.md", "Hand Built", "haystack body");
  db.pragma(`user_version = ${userVersion}`);
  db.close();
  return dbPath;
}

function readUserVersion(dbPath: string): number {
  const db = new Database(dbPath, { readonly: true });
  try {
    return db.pragma("user_version", { simple: true }) as number;
  } finally {
    db.close();
  }
}

beforeAll(() => {
  envSnapshot.SCHIST_INGEST_BIN = process.env.SCHIST_INGEST_BIN;
});
afterAll(async () => {
  if (envSnapshot.SCHIST_INGEST_BIN === undefined) delete process.env.SCHIST_INGEST_BIN;
  else process.env.SCHIST_INGEST_BIN = envSnapshot.SCHIST_INGEST_BIN;
  for (const dir of createdDirs) {
    await fs.rm(dir, { recursive: true, force: true });
  }
});

beforeEach(() => {
  resetSchemaCacheForTesting();
});

describe("index schema version staleness (#130 D3)", () => {
  test(
    "stale user_version forces a rebuild that restamps the current version",
    async () => {
      await useLocalSchistIngestBin();
      const vault = await makeVault();
      const dbPath = await buildDbAtVersion(vault, INDEX_SCHEMA_VERSION + 1);

      const res = sqliteReader.searchNotes(vault, "haystack");
      // The rebuild re-scans markdown: the vault's real note appears, the
      // hand-seeded row (not backed by a file) is gone.
      expect(res.find((r) => r.id === "notes/2026-07-06-version.md")).toBeDefined();
      expect(res.find((r) => r.id === "notes/hand-built.md")).toBeUndefined();
      expect(readUserVersion(dbPath)).toBe(INDEX_SCHEMA_VERSION);
    },
    60_000,
  );

  test(
    "deployment skew: an old ingest that restamps the old version yields the actionable typed error after ONE rebuild attempt",
    async () => {
      const log = await useNoopSchistIngestBin();
      const vault = await makeVault();
      await buildDbAtVersion(vault, INDEX_SCHEMA_VERSION + 1);

      // Rebuild-once → recheck → typed error. The message must say what to
      // actually do — upgrade schist-ingest — not loop or throw `no such column`.
      expect(() => sqliteReader.searchNotes(vault, "haystack")).toThrow(
        /schist-ingest is older than this MCP server[\s\S]*uv tool install/,
      );
      expect((await fs.readFile(log, "utf-8")).trim().split("\n")).toHaveLength(1);

      // Each subsequent tool call retries once and re-throws the same
      // actionable error — no uninformative loop within a call.
      expect(() => sqliteReader.searchNotes(vault, "haystack")).toThrow(
        /schist-ingest is older than this MCP server/,
      );
      expect((await fs.readFile(log, "utf-8")).trim().split("\n")).toHaveLength(2);
    },
    60_000,
  );

  test("user_version=0 (in-flight ingest or pre-marker DB) is exempt from the version check", async () => {
    await useFailLoudSchistIngestBin();
    const vault = await makeVault();
    await buildDbAtVersion(vault, 0);

    // Any rebuild attempt would hit the fail-loud bin and throw.
    const res = sqliteReader.searchNotes(vault, "haystack");
    expect(res.find((r) => r.id === "notes/hand-built.md")).toBeDefined();
  });

  test("current user_version passes without a rebuild", async () => {
    await useFailLoudSchistIngestBin();
    const vault = await makeVault();
    await buildDbAtVersion(vault, INDEX_SCHEMA_VERSION);

    const res = sqliteReader.searchNotes(vault, "haystack");
    expect(res.find((r) => r.id === "notes/hand-built.md")).toBeDefined();
  });
});

describe("required-tables parity fix (#339)", () => {
  test(
    "a DB missing the docs table rebuilds instead of failing the first docs query",
    async () => {
      await useLocalSchistIngestBin();
      const vault = await makeVault();
      // The exact pre-#339 drift shape: side tables present, docs absent —
      // the old TS required set {paper_metadata, concept_aliases} passed
      // this DB and the first docs SELECT then threw `no such table`.
      const dbDir = path.join(vault, ".schist");
      await fs.mkdir(dbDir, { recursive: true });
      const db = new Database(path.join(dbDir, "schist.db"));
      db.exec(`
        CREATE TABLE paper_metadata (doc_id TEXT PRIMARY KEY);
        CREATE TABLE concept_aliases (
          duplicate_slug TEXT NOT NULL, canonical_slug TEXT NOT NULL,
          reason TEXT, created_by TEXT NOT NULL,
          PRIMARY KEY (duplicate_slug, canonical_slug)
        );
      `);
      db.close();

      const res = sqliteReader.searchNotes(vault, "haystack");
      expect(res.find((r) => r.id === "notes/2026-07-06-version.md")).toBeDefined();
    },
    60_000,
  );
});
