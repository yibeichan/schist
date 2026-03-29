/**
 * Tests for schist-memory CLI commands
 * Uses a temp DB for isolation.
 */
import { execSync } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CLI = path.resolve(__dirname, "../dist/cli/memory-cli.js");

function run(args: string, env: Record<string, string> = {}): { stdout: string; stderr: string; code: number } {
  try {
    const stdout = execSync(`node ${CLI} ${args}`, {
      encoding: "utf8",
      env: { ...process.env, ...env },
    });
    return { stdout, stderr: "", code: 0 };
  } catch (e: unknown) {
    const err = e as { stdout?: string; stderr?: string; status?: number };
    return { stdout: err.stdout ?? "", stderr: err.stderr ?? "", code: err.status ?? 1 };
  }
}

describe("schist-memory CLI", () => {
  let tmpDb: string;

  beforeEach(() => {
    tmpDb = path.join(os.tmpdir(), `schist-test-${Date.now()}.db`);
  });

  afterEach(() => {
    if (fs.existsSync(tmpDb)) fs.unlinkSync(tmpDb);
    if (fs.existsSync(`${tmpDb}-wal`)) fs.unlinkSync(`${tmpDb}-wal`);
    if (fs.existsSync(`${tmpDb}-shm`)) fs.unlinkSync(`${tmpDb}-shm`);
  });

  describe("add-memory", () => {
    it("adds a memory entry and returns id", () => {
      const env = { SCHIST_MEMORY_DB: tmpDb, SCHIST_AGENT_ID: "sansan" };
      const result = run(`add-memory --agent sansan --type decision "test decision content"`, env);
      expect(result.code).toBe(0);
      expect(result.stdout).toContain("Memory entry added");
      expect(result.stdout).toContain("id=1");
    });

    it("rejects missing --agent", () => {
      const env = { SCHIST_MEMORY_DB: tmpDb };
      const result = run(`add-memory --type decision "content"`, env);
      expect(result.code).toBe(1);
      expect(result.stderr).toContain("--agent is required");
    });

    it("rejects missing --type", () => {
      const env = { SCHIST_MEMORY_DB: tmpDb, SCHIST_AGENT_ID: "sansan" };
      const result = run(`add-memory --agent sansan "content"`, env);
      expect(result.code).toBe(1);
      expect(result.stderr).toContain("--type is required");
    });

    it("rejects invalid entry_type", () => {
      const env = { SCHIST_MEMORY_DB: tmpDb, SCHIST_AGENT_ID: "sansan" };
      const result = run(`add-memory --agent sansan --type badtype "content"`, env);
      expect(result.code).toBe(1);
      expect(result.stderr).toContain("must be one of");
    });

    it("accepts all valid entry types", () => {
      const env = { SCHIST_MEMORY_DB: tmpDb, SCHIST_AGENT_ID: "sansan" };
      for (const type of ["decision", "lesson", "blocker", "completion", "observation"]) {
        const result = run(`add-memory --agent sansan --type ${type} "test"`, env);
        expect(result.code).toBe(0);
      }
    });

    it("stores tags and source_ref", () => {
      const env = { SCHIST_MEMORY_DB: tmpDb, SCHIST_AGENT_ID: "eleven" };
      const result = run(
        `add-memory --agent eleven --type lesson "lesson with meta" --tags "a,b,c" --ref "PR#42"`,
        env
      );
      expect(result.code).toBe(0);
    });
  });

  describe("search", () => {
    it("returns no results for empty DB", () => {
      const env = { SCHIST_MEMORY_DB: tmpDb };
      const result = run(`search "anything"`, env);
      expect(result.code).toBe(0);
      expect(result.stdout).toContain("No results");
    });

    it("finds added entry by content", () => {
      const env = { SCHIST_MEMORY_DB: tmpDb, SCHIST_AGENT_ID: "ninjia" };
      run(`add-memory --agent ninjia --type blocker "XSS vulnerability in kiosk input"`, env);
      const result = run(`search "kiosk"`, env);
      expect(result.code).toBe(0);
      expect(result.stdout).toContain("kiosk");
      expect(result.stdout).toContain("ninjia");
    });

    it("filters by --agent", () => {
      const env = { SCHIST_MEMORY_DB: tmpDb, SCHIST_AGENT_ID: "sansan" };
      run(`add-memory --agent sansan --type decision "sansan decision"`, env);
      const result = run(`search "decision" --agent sansan`, env);
      expect(result.stdout).toContain("sansan");
    });
  });

  describe("state get", () => {
    it("returns (not set) for missing key", () => {
      const env = { SCHIST_MEMORY_DB: tmpDb };
      const result = run(`state get sansan.missing_key`, env);
      expect(result.code).toBe(0);
      expect(result.stdout).toContain("not set");
    });

    it("errors on missing key arg", () => {
      const env = { SCHIST_MEMORY_DB: tmpDb };
      const result = run(`state get`, env);
      expect(result.code).toBe(1);
      expect(result.stderr).toContain("key is required");
    });
  });

  describe("state set", () => {
    it("sets and retrieves a string value", () => {
      const env = { SCHIST_MEMORY_DB: tmpDb, SCHIST_AGENT_ID: "sansan" };
      run(`state set sansan.current_pr '"PR #247"' --agent sansan`, env);
      const result = run(`state get sansan.current_pr`, env);
      expect(result.code).toBe(0);
      expect(result.stdout).toContain("PR #247");
      expect(result.stdout).toContain("sansan");
    });

    it("sets a JSON value", () => {
      const env = { SCHIST_MEMORY_DB: tmpDb, SCHIST_AGENT_ID: "eleven" };
      run(`state set eleven.blockers '[1,2,3]' --agent eleven`, env);
      const result = run(`state get eleven.blockers`, env);
      expect(result.code).toBe(0);
      // SQLite stores JSON as serialized string; value column shows parsed content
      expect(result.stdout).toContain("1,2,3");
    });

    it("rejects wrong key prefix for agent", () => {
      const env = { SCHIST_MEMORY_DB: tmpDb, SCHIST_AGENT_ID: "sansan" };
      const result = run(`state set ninjia.key "value" --agent sansan`, env);
      expect(result.code).toBe(1);
      expect(result.stderr).toContain("prefix");
    });

    it("errors on missing --agent", () => {
      const env = { SCHIST_MEMORY_DB: tmpDb };
      const result = run(`state set sansan.key "value"`, env);
      expect(result.code).toBe(1);
      expect(result.stderr).toContain("--agent is required");
    });
  });

  describe("unknown command", () => {
    it("exits 1 for unknown command", () => {
      const result = run(`unknown-command`);
      expect(result.code).toBe(1);
      expect(result.stderr).toContain("unknown command");
    });
  });
});
