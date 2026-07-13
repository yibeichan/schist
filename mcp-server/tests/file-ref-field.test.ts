/**
 * file_ref — optional external file pointer on docs.
 *
 * schist indexes the pointer but does not store, sync, validate, or manage the
 * referenced file.
 */
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import { execFile as execFileCb } from "child_process";
import { promisify } from "util";
import { fileURLToPath } from "url";
import Database from "better-sqlite3";
import { create_note, get_note, loadVaultConfig } from "../src/tools.js";
import { resetSchemaCacheForTesting } from "../src/sqlite-reader.js";
import * as sqliteReader from "../src/sqlite-reader.js";
import { localIngestWrapperScript } from "./local-ingest-bin.js";

const execFile = promisify(execFileCb);
const TEST_AGENT = "test-agent";
const createdDirs = new Set<string>();
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "..", "..");
const envSnapshot: Record<string, string | undefined> = {};

async function useLocalSchistIngestBin(): Promise<void> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-ingest-bin-"));
  createdDirs.add(dir);
  const bin = path.join(dir, "schist-ingest-local");
  await fs.writeFile(bin, localIngestWrapperScript(repoRoot), { mode: 0o755 });
  process.env.SCHIST_INGEST_BIN = bin;
}

async function makeTempVault(prefix = "schist-file-ref-"): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), prefix));
  createdDirs.add(dir);
  await execFile("git", ["init"], { cwd: dir });
  await execFile("git", ["config", "user.email", "test@test.com"], { cwd: dir });
  await execFile("git", ["config", "user.name", "Test"], { cwd: dir });
  await fs.writeFile(
    path.join(dir, "schist.yaml"),
    [
      "name: Test Vault",
      "write_branch: drafts",
      "directories:",
      "  - notes",
      "statuses:",
      "  - draft",
      "connection_types:",
      "  - related",
      "",
    ].join("\n"),
  );
  await execFile("git", ["add", "."], { cwd: dir });
  await execFile("git", ["commit", "-m", "init"], { cwd: dir });
  return dir;
}

beforeAll(() => {
  envSnapshot.SCHIST_AGENT_ID = process.env.SCHIST_AGENT_ID;
  envSnapshot.SCHIST_INGEST_BIN = process.env.SCHIST_INGEST_BIN;
  process.env.SCHIST_AGENT_ID = TEST_AGENT;
});

afterAll(async () => {
  if (envSnapshot.SCHIST_AGENT_ID === undefined) delete process.env.SCHIST_AGENT_ID;
  else process.env.SCHIST_AGENT_ID = envSnapshot.SCHIST_AGENT_ID;
  if (envSnapshot.SCHIST_INGEST_BIN === undefined) delete process.env.SCHIST_INGEST_BIN;
  else process.env.SCHIST_INGEST_BIN = envSnapshot.SCHIST_INGEST_BIN;
  for (const dir of createdDirs) {
    await fs.rm(dir, { recursive: true, force: true });
  }
});

describe("create_note file_ref frontmatter", () => {
  test("writes file_ref to frontmatter", async () => {
    const vault = await makeTempVault();
    const config = await loadVaultConfig(vault);
    const result = (await create_note(
      vault,
      {
        owner: TEST_AGENT,
        title: "With File Ref",
        body: "body",
        file_ref: "/mnt/data/papers/example.pdf",
      },
      config,
    )) as { path: string };

    const content = await fs.readFile(path.join(vault, result.path), "utf-8");
    expect(content).toMatch(/^file_ref: \/mnt\/data\/papers\/example\.pdf$/m);
  });

  test("omits file_ref from frontmatter when not declared", async () => {
    const vault = await makeTempVault();
    const config = await loadVaultConfig(vault);
    const result = (await create_note(
      vault,
      { owner: TEST_AGENT, title: "No File Ref", body: "body" },
      config,
    )) as { path: string };

    const content = await fs.readFile(path.join(vault, result.path), "utf-8");
    expect(content).not.toMatch(/^file_ref:/m);
  });
});

describe("get_note file_ref round-trip", () => {
  test("returns file_ref when declared on the note", async () => {
    const vault = await makeTempVault();
    const config = await loadVaultConfig(vault);
    const written = (await create_note(
      vault,
      {
        owner: TEST_AGENT,
        title: "Round Trip File Ref",
        body: "body",
        file_ref: "/mnt/data/round-trip.pdf",
      },
      config,
    )) as { id: string };

    const fetched = (await get_note(vault, { id: written.id })) as { file_ref?: string };
    expect(fetched.file_ref).toBe("/mnt/data/round-trip.pdf");
  });
});

describe("schema-drift auto-rebuild for file_ref", () => {
  beforeEach(() => {
    resetSchemaCacheForTesting();
  });

  test("queryGraph rebuilds when docs.file_ref is missing", async () => {
    await useLocalSchistIngestBin();
    const vault = await makeTempVault("schist-file-ref-drift-");
    await fs.mkdir(path.join(vault, "notes"), { recursive: true });
    await fs.writeFile(
      path.join(vault, "notes", "2026-06-08-file-ref.md"),
      "---\ntitle: File Ref Drift\ndate: 2026-06-08\nfile_ref: /mnt/data/drift.pdf\n---\n\nhaystack body.\n",
    );
    await execFile("git", ["add", "."], { cwd: vault });
    await execFile("git", ["commit", "-m", "seed file ref"], { cwd: vault });

    const dbDir = path.join(vault, ".schist");
    await fs.mkdir(dbDir, { recursive: true });
    const db = new Database(path.join(dbDir, "schist.db"));
    db.exec(`
      CREATE TABLE docs (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        date TEXT,
        status TEXT DEFAULT 'draft',
        tags TEXT,
        concepts TEXT,
        body TEXT NOT NULL DEFAULT '',
        scope TEXT DEFAULT 'global',
        source TEXT,
        confidence TEXT
        -- NO file_ref column — pre-file_ref schema
      );
      CREATE VIRTUAL TABLE docs_fts USING fts5(
        title, body, tags, scope UNINDEXED,
        content='docs', content_rowid='rowid'
      );
      CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
        INSERT INTO docs_fts(rowid, title, body, tags, scope)
        VALUES (new.rowid, new.title, new.body, new.tags, new.scope);
      END;
    `);
    db.prepare(
      "INSERT INTO docs (id, title, body, scope, confidence) VALUES (?, ?, ?, 'global', NULL)",
    ).run("notes/stale.md", "Stale", "haystack body");
    db.close();

    const preDb = new Database(path.join(vault, ".schist", "schist.db"), { readonly: true });
    const preCols = (preDb.pragma("table_info(docs)") as Array<{ name: string }>).map((c) => c.name);
    preDb.close();
    expect(preCols).not.toContain("file_ref");

    const result = await sqliteReader.queryGraph(vault, "SELECT id, file_ref FROM docs WHERE id LIKE 'notes/%'");
    expect(result.columns).toEqual(["id", "file_ref"]);
    expect(result.rows).toContainEqual(["notes/2026-06-08-file-ref.md", "/mnt/data/drift.pdf"]);

    const postDb = new Database(path.join(vault, ".schist", "schist.db"), { readonly: true });
    const postCols = (postDb.pragma("table_info(docs)") as Array<{ name: string }>).map((c) => c.name);
    postDb.close();
    expect(postCols).toContain("file_ref");
  });
});
