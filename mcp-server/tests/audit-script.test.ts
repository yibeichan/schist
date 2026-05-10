import { describe, it, expect, beforeAll, afterAll } from "@jest/globals";
import { measureResponse, runAudit } from "../../scripts/audit_mcp_response_sizes.js";
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import { execSync } from "child_process";

describe("measureResponse", () => {
  it("returns byte length of JSON-serialized response", () => {
    const result = measureResponse({ id: "x", title: "y", snippet: "z" });
    // {"id":"x","title":"y","snippet":"z"} = 36 bytes
    expect(result.bytes).toBe(36);
  });

  it("returns approximate token count using 4-bytes-per-token heuristic", () => {
    const result = measureResponse({ a: "x".repeat(40) });
    // {"a":"xxxx...xxxx"} = 48 bytes ≈ 12 tokens
    expect(result.approxTokens).toBe(12);
  });

  it("handles array responses (e.g. searchNotes return)", () => {
    const result = measureResponse([{ id: "a" }, { id: "b" }]);
    // [{"id":"a"},{"id":"b"}] = 23 bytes
    expect(result.bytes).toBe(23);
    expect(result.entryCount).toBe(2);
  });

  it("reports entryCount: 1 for non-array responses", () => {
    const result = measureResponse({ noteCount: 0 });
    expect(result.entryCount).toBe(1);
  });
});

describe("runAudit (end-to-end)", () => {
  let tmpVault: string;
  let tmpMemoryDb: string;
  // Snapshot of any SCHIST_* env vars present at suite start. Restored
  // verbatim in afterAll so the suite is hermetic against the surrounding
  // shell (developer machines often have SCHIST_AGENT_NAME / SCHIST_IDENTITY
  // set; without isolation those leak into searchNotes scope-inherit, the
  // ingest script's identity resolution, etc).
  const schistEnvKeys = [
    "SCHIST_AGENT_ID",
    "SCHIST_AGENT_NAME",
    "SCHIST_IDENTITY",
    "SCHIST_INGEST_SCRIPT",
    "SCHIST_MEMORY_DB",
    "SCHIST_VAULT_PATH",
  ] as const;
  const originalEnv: Record<string, string | undefined> = {};

  // Inherit stderr so ENOENT / permission errors from the spawned schist /
  // schist-ingest binaries surface in the test output instead of being
  // swallowed by the failed-execSync wrapper.
  const stdio: ["pipe", "pipe", "inherit"] = ["pipe", "pipe", "inherit"];

  beforeAll(async () => {
    for (const k of schistEnvKeys) {
      originalEnv[k] = process.env[k];
      delete process.env[k];
    }
    tmpVault = await fs.mkdtemp(path.join(os.tmpdir(), "schist-audit-"));
    tmpMemoryDb = path.join(tmpVault, "agent-state.db");
    process.env.SCHIST_MEMORY_DB = tmpMemoryDb;

    execSync(`schist init ${tmpVault} --name audit-test`, { stdio });
    for (let i = 0; i < 5; i++) {
      const noteFile = path.join(tmpVault, "notes", `2026-05-04-fixture-${i}.md`);
      await fs.mkdir(path.dirname(noteFile), { recursive: true });
      await fs.writeFile(
        noteFile,
        `---\ntitle: Fixture ${i}\ndate: 2026-05-04\nstatus: draft\ntags: [audit]\n---\n\nBody for fixture note ${i}, ${"x".repeat(200)}.\n`
      );
    }
    execSync(`git -C ${tmpVault} add -A && git -C ${tmpVault} commit -m fixtures`, { stdio });
    execSync(`schist-ingest --vault ${tmpVault} --db ${tmpVault}/.schist/schist.db`, { stdio });
  });

  afterAll(async () => {
    for (const k of schistEnvKeys) {
      if (originalEnv[k] === undefined) delete process.env[k];
      else process.env[k] = originalEnv[k];
    }
    await fs.rm(tmpVault, { recursive: true, force: true });
  });

  it("produces a report covering every tool in the audit set", async () => {
    const report = await runAudit({ vault: tmpVault });
    expect(report.tools).toEqual(
      expect.arrayContaining([
        "search_notes",
        "list_concepts",
        "list_domains",
        "query_graph",
        "get_context_minimal",
        "get_context_standard",
        "get_context_full",
        "search_memory",
      ])
    );
  });

  it("reports search_notes byte count > 0 against fixture vault", async () => {
    const report = await runAudit({ vault: tmpVault });
    const sn = report.measurements.search_notes;
    expect(sn.bytes).toBeGreaterThan(0);
    expect(sn.entryCount).toBeGreaterThanOrEqual(1);
  });

  it("returns non-error responses for read-side tools (catches broken native bindings)", async () => {
    // Without this, the audit "succeeds" even when better-sqlite3 fails to
    // load and every tool returns { error, message } — measureResponse
    // dutifully measures the error envelope and bytes>0/entryCount>=1
    // both still pass. Probe one tool per distinct DB / executor path so
    // a broken binding can't silently fail any subset:
    //   - list_concepts / list_domains / search_memory: array return
    //   - query_graph:                                   { columns, rows, rowCount }
    //   - search_memory:                                  separate memory DB file
    const tools = await import("../../mcp-server/dist/tools.js");
    type ShapeCheck = (resp: unknown) => boolean;
    const isArrayShape: ShapeCheck = (r) => Array.isArray(r);
    const isQueryGraphShape: ShapeCheck = (r) =>
      !!r && typeof r === "object" && Array.isArray((r as { rows?: unknown }).rows);
    const probes: Array<[string, unknown, ShapeCheck]> = [
      ["list_concepts", await tools.list_concepts(tmpVault, {}), isArrayShape],
      ["list_domains", await tools.list_domains(tmpVault, {}), isArrayShape],
      ["query_graph", await tools.query_graph(tmpVault, { sql: "SELECT 1 AS x" }), isQueryGraphShape],
      ["search_memory", await tools.search_memory(tmpVault, { limit: 1 }), isArrayShape],
    ];
    for (const [name, resp, shapeOk] of probes) {
      if (resp && typeof resp === "object" && "error" in resp) {
        const err = resp as { error: string; message?: string };
        throw new Error(
          `${name} returned error envelope, indicating a broken sqlite stack: ` +
          `${err.error}: ${err.message ?? ""}`
        );
      }
      expect(shapeOk(resp)).toBe(true);
    }
  });

  it("honors searchQuery override (not just the default)", async () => {
    // The default "fixture" path is exercised by the two specs above. This
    // pins the override pathway so a future refactor can't silently drop
    // the parameter — e.g. by accidentally re-hardcoding "fixture" inside
    // runAudit. The fixture corpus contains no "nonexistent-token-zzz"
    // notes, so search_notes must return an empty result (0 entries) when
    // the override is honored.
    const report = await runAudit({ vault: tmpVault, searchQuery: "nonexistent-token-zzz" });
    expect(report.measurements.search_notes.entryCount).toBe(0);
  });
});
