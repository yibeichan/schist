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
  let originalMemoryDb: string | undefined;

  beforeAll(async () => {
    tmpVault = await fs.mkdtemp(path.join(os.tmpdir(), "schist-audit-"));
    tmpMemoryDb = path.join(tmpVault, "agent-state.db");

    // Isolate the memory DB so the test never touches the user's real
    // ~/.openclaw/memory/agent-state.db. Stash the prior value so afterAll
    // can restore the env exactly as we found it.
    originalMemoryDb = process.env.SCHIST_MEMORY_DB;
    process.env.SCHIST_MEMORY_DB = tmpMemoryDb;

    execSync(`schist init ${tmpVault} --name audit-test`, { stdio: "pipe" });
    for (let i = 0; i < 5; i++) {
      const noteFile = path.join(tmpVault, "notes", `2026-05-04-fixture-${i}.md`);
      await fs.mkdir(path.dirname(noteFile), { recursive: true });
      await fs.writeFile(
        noteFile,
        `---\ntitle: Fixture ${i}\ndate: 2026-05-04\nstatus: draft\ntags: [audit]\n---\n\nBody for fixture note ${i}, ${"x".repeat(200)}.\n`
      );
    }
    execSync(`git -C ${tmpVault} add -A && git -C ${tmpVault} commit -m fixtures`, { stdio: "pipe" });
    execSync(`schist-ingest --vault ${tmpVault} --db ${tmpVault}/.schist/schist.db`, { stdio: "pipe" });
  });

  afterAll(async () => {
    if (originalMemoryDb === undefined) delete process.env.SCHIST_MEMORY_DB;
    else process.env.SCHIST_MEMORY_DB = originalMemoryDb;
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
    // both still pass. Probe two read tools that should always return
    // arrays on a fresh fixture vault.
    const tools = await import("../../mcp-server/dist/tools.js");
    const probes: Array<[string, unknown]> = [
      ["list_concepts", await tools.list_concepts(tmpVault, {})],
      ["list_domains", await tools.list_domains(tmpVault, {})],
    ];
    for (const [name, resp] of probes) {
      if (resp && typeof resp === "object" && "error" in resp) {
        const err = resp as { error: string; message?: string };
        throw new Error(
          `${name} returned error envelope, indicating a broken sqlite stack: ` +
          `${err.error}: ${err.message ?? ""}`
        );
      }
      expect(Array.isArray(resp)).toBe(true);
    }
  });
});
