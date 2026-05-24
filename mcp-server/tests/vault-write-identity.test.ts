/**
 * #63 — vault-write tools (create_note, add_connection, assign_domain) must
 * call validateOwner on each call AND stamp the validated identity onto
 * frontmatter (source_agent) + the git commit message.
 *
 * Tests in tools.test.ts exercise the happy path with a single fixed identity
 * via a process-level SCHIST_AGENT_ID. Here we cover:
 *   1. Missing/empty owner → CONFIG/VALIDATION error
 *   2. Owner not in allowlist → VALIDATION error
 *   3. Valid owner → propagates to source_agent frontmatter + commit message
 */
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import { execFile as execFileCb } from "child_process";
import { promisify } from "util";
import Database from "better-sqlite3";
import {
  loadVaultConfig,
  create_note,
  add_connection,
  assign_domain,
} from "../src/tools.js";

const execFile = promisify(execFileCb);

async function makeTempVault(extraYaml = ""): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-vw-identity-"));
  await execFile("git", ["init"], { cwd: dir });
  await execFile("git", ["config", "user.email", "test@test.com"], { cwd: dir });
  await execFile("git", ["config", "user.name", "Test"], { cwd: dir });
  const yaml = [
    "name: Test Vault",
    "write_branch: drafts",
    "directories:",
    "  - notes",
    "statuses:",
    "  - draft",
    "connection_types:",
    "  - related",
    extraYaml,
  ]
    .filter(Boolean)
    .join("\n") + "\n";
  await fs.writeFile(path.join(dir, "schist.yaml"), yaml);
  await execFile("git", ["add", "."], { cwd: dir });
  await execFile("git", ["commit", "-m", "init"], { cwd: dir });
  return dir;
}

async function makeVaultWithDomains(domains: string[]): Promise<string> {
  const vault = await makeTempVault(
    `\ndomains:\n${domains.map((d) => `  - ${d}`).join("\n")}`
  );
  const dbPath = path.join(vault, ".schist", "schist.db");
  await fs.mkdir(path.dirname(dbPath), { recursive: true });
  const db = new Database(dbPath);
  db.exec(
    "CREATE TABLE domains (slug TEXT PRIMARY KEY, label TEXT NOT NULL, description TEXT, parent_slug TEXT REFERENCES domains(slug))"
  );
  const insert = db.prepare("INSERT INTO domains (slug, label) VALUES (?, ?)");
  for (const d of domains) insert.run(d, d);
  db.close();
  return vault;
}

describe("#63 vault-write identity enforcement", () => {
  beforeEach(() => {
    delete process.env.SCHIST_AGENT_ID;
    delete process.env.SCHIST_ALLOWED_AGENTS;
  });
  afterEach(() => {
    delete process.env.SCHIST_AGENT_ID;
    delete process.env.SCHIST_ALLOWED_AGENTS;
  });

  // -------------------------------------------------------------------------
  // CONFIG_ERROR when no identity env is set (regardless of args.owner)
  // -------------------------------------------------------------------------

  describe("CONFIG_ERROR when neither SCHIST_AGENT_ID nor SCHIST_ALLOWED_AGENTS is set", () => {
    test("create_note returns CONFIG_ERROR", async () => {
      const vault = await makeTempVault();
      const config = await loadVaultConfig(vault);
      const result = (await create_note(
        vault,
        { owner: "anything", title: "X", body: "y" },
        config,
      )) as { error: string; message: string };
      expect(result.error).toBe("CONFIG_ERROR");
      expect(result.message).toMatch(/SCHIST_AGENT_ID or SCHIST_ALLOWED_AGENTS/);
    });

    test("add_connection returns CONFIG_ERROR", async () => {
      const vault = await makeTempVault();
      const result = (await add_connection(vault, {
        owner: "anything",
        source: "notes/missing.md",
        target: "t",
        type: "related",
      })) as { error: string; message: string };
      expect(result.error).toBe("CONFIG_ERROR");
    });

    test("assign_domain returns CONFIG_ERROR", async () => {
      const vault = await makeVaultWithDomains(["ai"]);
      const result = (await assign_domain(vault, {
        owner: "anything",
        id: "notes/missing.md",
        domain: "ai",
      })) as { error: string; message: string };
      expect(result.error).toBe("CONFIG_ERROR");
    });
  });

  // -------------------------------------------------------------------------
  // VALIDATION_ERROR when owner doesn't match env (single-agent mode)
  // -------------------------------------------------------------------------

  describe("VALIDATION_ERROR when owner does not match SCHIST_AGENT_ID", () => {
    test("create_note rejects mismatched owner", async () => {
      process.env.SCHIST_AGENT_ID = "octopus";
      const vault = await makeTempVault();
      const config = await loadVaultConfig(vault);
      const result = (await create_note(
        vault,
        { owner: "atwood", title: "X", body: "y" },
        config,
      )) as { error: string; message: string };
      expect(result.error).toBe("VALIDATION_ERROR");
      expect(result.message).toMatch(/Owner 'atwood' does not match SCHIST_AGENT_ID 'octopus'/);
    });

    test("add_connection rejects mismatched owner before doing any IO", async () => {
      // No source note exists; if identity gate runs first we get VALIDATION_ERROR,
      // not NOT_FOUND. This proves the gate fires before vault reads.
      process.env.SCHIST_AGENT_ID = "octopus";
      const vault = await makeTempVault();
      const result = (await add_connection(vault, {
        owner: "atwood",
        source: "notes/never-existed.md",
        target: "t",
        type: "related",
      })) as { error: string; message: string };
      expect(result.error).toBe("VALIDATION_ERROR");
    });

    test("assign_domain rejects mismatched owner before listing domains", async () => {
      process.env.SCHIST_AGENT_ID = "octopus";
      // No .schist/schist.db at all — listDomains would otherwise throw.
      const vault = await makeTempVault("\ndomains:\n  - ai\n");
      const result = (await assign_domain(vault, {
        owner: "atwood",
        id: "notes/n.md",
        domain: "ai",
      })) as { error: string; message: string };
      expect(result.error).toBe("VALIDATION_ERROR");
    });

    test("empty-string owner is rejected (single-agent mode)", async () => {
      process.env.SCHIST_AGENT_ID = "octopus";
      const vault = await makeTempVault();
      const config = await loadVaultConfig(vault);
      const result = (await create_note(
        vault,
        { owner: "", title: "X", body: "y" },
        config,
      )) as { error: string };
      expect(result.error).toBe("VALIDATION_ERROR");
    });

    test("whitespace-only owner is rejected (allowlist mode)", async () => {
      // allowedAgents.split(",").map(trim).filter(Boolean) drops empty entries,
      // so a whitespace-only owner is never in the allowlist.
      process.env.SCHIST_ALLOWED_AGENTS = "octopus,atwood";
      const vault = await makeTempVault();
      const config = await loadVaultConfig(vault);
      const result = (await create_note(
        vault,
        { owner: "   ", title: "X", body: "y" },
        config,
      )) as { error: string };
      expect(result.error).toBe("VALIDATION_ERROR");
    });

    test("padded owner is accepted; canonical form stamped on both frontmatter and commit", async () => {
      // End-to-end of the #131 review-time trim-symmetry fix: caller sends
      // "atwood " (trailing space), validateOwner canonicalizes to "atwood",
      // and both source_agent and the commit subject store "atwood" (NOT
      // "atwood "). Without canonicalization the agent's writes would
      // silently split across two distinct owner keys in the side tables.
      process.env.SCHIST_ALLOWED_AGENTS = "octopus,atwood";
      const vault = await makeTempVault();
      const config = await loadVaultConfig(vault);
      const result = (await create_note(
        vault,
        { owner: "atwood ", title: "Padded owner", body: "body" },
        config,
      )) as { path: string; commitSha: string };
      expect(result.commitSha).toBeDefined();

      const content = await fs.readFile(path.join(vault, result.path), "utf-8");
      // The YAML line must be exactly `source_agent: atwood` with no inner
      // whitespace bracketed by the trailing space the caller sent. The
      // `$` in multiline mode matches end-of-line, so trailing-space-then-
      // newline would fail the assertion.
      expect(content).toMatch(/^source_agent: atwood$/m);

      const { stdout } = await execFile("git", ["log", "-1", "--format=%s"], { cwd: vault });
      // git's `--format=%s` strips trailing newline but not interior ones;
      // an end-anchored `— by atwood` proves no padding leaked through.
      expect(stdout.trim()).toMatch(/— by atwood$/);
    });
  });

  // -------------------------------------------------------------------------
  // Allowlist mode — multi-agent shared MCP deployment
  // -------------------------------------------------------------------------

  describe("allowlist mode (SCHIST_ALLOWED_AGENTS)", () => {
    test("create_note accepts an allowlisted owner and stamps source_agent", async () => {
      process.env.SCHIST_ALLOWED_AGENTS = "octopus,atwood,eleven";
      const vault = await makeTempVault();
      const config = await loadVaultConfig(vault);
      const result = (await create_note(
        vault,
        { owner: "atwood", title: "Note by atwood", body: "body" },
        config,
      )) as { id: string; path: string; commitSha: string };
      expect(result.commitSha).toBeDefined();

      const content = await fs.readFile(path.join(vault, result.path), "utf-8");
      expect(content).toMatch(/^source_agent:\s*atwood$/m);

      // Commit message includes the agent — git log subject of HEAD.
      const { stdout } = await execFile("git", ["log", "-1", "--format=%s"], { cwd: vault });
      expect(stdout.trim()).toMatch(/^feat\(schist\): write Note by atwood — by atwood$/);
    });

    test("source_agent frontmatter matches the agent in the commit subject", async () => {
      // Defense-in-depth: validateOwner currently demands exact env match,
      // so the value stamped on `source_agent: <owner>` and the value
      // following "— by " in the commit subject MUST be identical. If a
      // future refactor weakens validateOwner (e.g. case-insensitive match,
      // trim-on-accept), this test fires before silent attribution drift
      // makes it into a release.
      process.env.SCHIST_ALLOWED_AGENTS = "octopus,atwood";
      const vault = await makeTempVault();
      const config = await loadVaultConfig(vault);
      const result = (await create_note(
        vault,
        { owner: "atwood", title: "Sync check", body: "body" },
        config,
      )) as { path: string };

      const content = await fs.readFile(path.join(vault, result.path), "utf-8");
      const sourceAgentMatch = content.match(/^source_agent:\s*(.+)$/m);
      expect(sourceAgentMatch).not.toBeNull();
      const sourceAgent = sourceAgentMatch![1].trim();

      const { stdout } = await execFile("git", ["log", "-1", "--format=%s"], { cwd: vault });
      const commitMatch = stdout.trim().match(/— by (.+)$/);
      expect(commitMatch).not.toBeNull();
      const commitOwner = commitMatch![1].trim();

      expect(sourceAgent).toBe(commitOwner);
    });

    test("create_note rejects an owner outside the allowlist", async () => {
      process.env.SCHIST_ALLOWED_AGENTS = "octopus,eleven";
      const vault = await makeTempVault();
      const config = await loadVaultConfig(vault);
      const result = (await create_note(
        vault,
        { owner: "atwood", title: "X", body: "y" },
        config,
      )) as { error: string; message: string };
      expect(result.error).toBe("VALIDATION_ERROR");
      expect(result.message).toMatch(/'atwood' not in SCHIST_ALLOWED_AGENTS/);
    });

    test("add_connection stamps commit message with the validated agent", async () => {
      process.env.SCHIST_ALLOWED_AGENTS = "octopus,atwood";
      const vault = await makeTempVault();
      const config = await loadVaultConfig(vault);

      // Seed a source note as octopus...
      const seed = (await create_note(
        vault,
        { owner: "octopus", title: "Seed", body: "body" },
        config,
      )) as { path: string };

      // ...then have atwood add a connection. Both commits should attribute
      // to the agent that made them — i.e. the connection commit must say
      // "by atwood", not "by octopus".
      await add_connection(vault, {
        owner: "atwood",
        source: seed.path,
        target: "some-target",
        type: "related",
      });

      const { stdout } = await execFile("git", ["log", "-2", "--format=%s"], { cwd: vault });
      const subjects = stdout.trim().split("\n");
      expect(subjects[0]).toMatch(/^feat\(schist\): write .* — by atwood$/);
      expect(subjects[1]).toMatch(/^feat\(schist\): write Seed — by octopus$/);
    });

    test("assign_domain stamps commit message with the validated agent", async () => {
      process.env.SCHIST_ALLOWED_AGENTS = "octopus,atwood";
      const vault = await makeVaultWithDomains(["ai"]);
      const config = await loadVaultConfig(vault);

      const seed = (await create_note(
        vault,
        { owner: "octopus", title: "Doc", body: "body" },
        config,
      )) as { path: string };

      await assign_domain(vault, {
        owner: "atwood",
        id: seed.path,
        domain: "ai",
      });

      const { stdout } = await execFile("git", ["log", "-1", "--format=%s"], { cwd: vault });
      // writeNote always prefixes "write" — the commitTitle here is the
      // tool-supplied "assign domain ai to <path>".
      expect(stdout.trim()).toMatch(/^feat\(schist\): write assign domain ai to .* — by atwood$/);
    });
  });
});
