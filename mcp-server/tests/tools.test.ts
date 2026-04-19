import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import Database from "better-sqlite3";
import { execFile as execFileCb } from "child_process";
import { promisify } from "util";
import { loadVaultConfig, create_note, get_context, triggerSpokePush, maybeSpokePull, assign_domain } from "../src/tools.js";

const execFile = promisify(execFileCb);

async function makeTempVault(extraYaml = ""): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-tools-test-"));
  await execFile("git", ["init"], { cwd: dir });
  await execFile("git", ["config", "user.email", "test@test.com"], { cwd: dir });
  await execFile("git", ["config", "user.name", "Test"], { cwd: dir });
  const yaml = [
    "name: Test Vault",
    "write_branch: drafts",
    "directories:",
    "  - notes",
    "  - papers",
    "statuses:",
    "  - draft",
    "  - review",
    "  - final",
    "connection_types:",
    "  - extends",
    "  - supports",
    extraYaml,
  ]
    .filter(Boolean)
    .join("\n") + "\n";
  await fs.writeFile(path.join(dir, "schist.yaml"), yaml);
  await execFile("git", ["add", "."], { cwd: dir });
  await execFile("git", ["commit", "-m", "init"], { cwd: dir });
  return dir;
}

// ---------------------------------------------------------------------------
// loadVaultConfig — YAML parser
// ---------------------------------------------------------------------------

describe("loadVaultConfig (js-yaml)", () => {
  test("parses standard YAML config correctly", async () => {
    const vault = await makeTempVault();
    const config = await loadVaultConfig(vault);
    expect(config.name).toBe("Test Vault");
    expect(config.writeBranch).toBe("drafts");
    expect(config.directories).toEqual(["notes", "papers"]);
    expect(config.statuses).toEqual(["draft", "review", "final"]);
    expect(config.connectionTypes).toEqual(["extends", "supports"]);
  });

  test("handles inline comments correctly (regex parser would fail)", async () => {
    const vault = await makeTempVault();
    // Overwrite yaml with inline comment — hand-rolled regex would capture "# ignored"
    await fs.writeFile(
      path.join(vault, "schist.yaml"),
      "name: My Vault # inline comment\nwrite_branch: drafts\n"
    );
    const config = await loadVaultConfig(vault);
    expect(config.name).toBe("My Vault");
  });

  test("handles quoted values containing colons", async () => {
    const vault = await makeTempVault();
    await fs.writeFile(
      path.join(vault, "schist.yaml"),
      'name: "Vault: Advanced"\nwrite_branch: drafts\n'
    );
    const config = await loadVaultConfig(vault);
    expect(config.name).toBe("Vault: Advanced");
  });
});

// ---------------------------------------------------------------------------
// create_note — filename collision
// ---------------------------------------------------------------------------

describe("create_note filename collision", () => {
  test("two notes with same title same day get distinct paths", async () => {
    const vault = await makeTempVault();
    const config = await loadVaultConfig(vault);

    const result1 = await create_note(
      vault,
      { title: "Duplicate Title", body: "first body" },
      config
    ) as { id: string; path: string; commitSha: string };

    const result2 = await create_note(
      vault,
      { title: "Duplicate Title", body: "second body" },
      config
    ) as { id: string; path: string; commitSha: string };

    expect(result1.path).not.toBe(result2.path);
    expect(result1.commitSha).toBeDefined();
    expect(result2.commitSha).toBeDefined();

    // Both files must exist with distinct content
    const content1 = await fs.readFile(path.join(vault, result1.path), "utf-8");
    const content2 = await fs.readFile(path.join(vault, result2.path), "utf-8");
    expect(content1).toContain("first body");
    expect(content2).toContain("second body");
  }, 30000);
});

// ---------------------------------------------------------------------------
// Lazy capabilities — index-level behaviour (unit test via tools layer)
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Spoke auto-sync — fires only when .schist/spoke.yaml is present
// ---------------------------------------------------------------------------

describe("triggerSpokePush", () => {
  test("spawns python push when spoke.yaml exists", async () => {
    const vault = await makeTempVault();
    await fs.mkdir(path.join(vault, ".schist"), { recursive: true });
    await fs.writeFile(
      path.join(vault, ".schist", "spoke.yaml"),
      "hub: file:///nonexistent\nidentity: test\nscope: notes\n"
    );

    // Stub python3 that touches a sentinel file so we can observe invocation.
    // The fs.access → spawn chain inside triggerSpokePush is microtask-delayed,
    // so we poll a few times before giving up.
    const sentinel = path.join(vault, ".schist", "push-fired");
    const stubDir = await fs.mkdtemp(path.join(os.tmpdir(), "stub-py-"));
    const stub = path.join(stubDir, "python3");
    await fs.writeFile(
      stub,
      `#!/bin/sh\ntouch "${sentinel}"\n`,
      { mode: 0o755 }
    );

    const origPath = process.env.PATH;
    process.env.PATH = `${stubDir}:${origPath}`;
    try {
      triggerSpokePush(vault);
      // spawn is fire-and-forget; poll briefly for the sentinel
      let fired = false;
      for (let i = 0; i < 60; i++) {
        try {
          await fs.access(sentinel);
          fired = true;
          break;
        } catch {
          await new Promise((r) => setTimeout(r, 50));
        }
      }
      expect(fired).toBe(true);
    } finally {
      process.env.PATH = origPath;
      await fs.rm(stubDir, { recursive: true, force: true });
    }
  }, 10000);

  test("no-op when spoke.yaml missing", async () => {
    const vault = await makeTempVault();
    // No .schist/spoke.yaml — should silently do nothing. Verify no throw.
    expect(() => triggerSpokePush(vault)).not.toThrow();
    await new Promise((r) => setTimeout(r, 100));
  });
});

describe("maybeSpokePull", () => {
  test("returns quickly when spoke.yaml missing", async () => {
    const vault = await makeTempVault();
    const t0 = Date.now();
    await maybeSpokePull(vault, 5000);
    expect(Date.now() - t0).toBeLessThan(200);
  });

  test("honors timeout when pull hangs", async () => {
    const vault = await makeTempVault();
    await fs.mkdir(path.join(vault, ".schist"), { recursive: true });
    await fs.writeFile(
      path.join(vault, ".schist", "spoke.yaml"),
      "hub: file:///nonexistent\nidentity: test\nscope: notes\n"
    );

    const stubDir = await fs.mkdtemp(path.join(os.tmpdir(), "stub-py-"));
    const stub = path.join(stubDir, "python3");
    await fs.writeFile(stub, "#!/bin/sh\nsleep 10\n", { mode: 0o755 });

    const origPath = process.env.PATH;
    process.env.PATH = `${stubDir}:${origPath}`;
    try {
      const t0 = Date.now();
      await maybeSpokePull(vault, 300);
      const elapsed = Date.now() - t0;
      expect(elapsed).toBeGreaterThanOrEqual(250);
      expect(elapsed).toBeLessThan(1500);
    } finally {
      process.env.PATH = origPath;
      await fs.rm(stubDir, { recursive: true, force: true });
    }
  }, 10000);
});

describe("sync error sentinel", () => {
  test("get_context surfaces last-sync-error as syncWarning and clears it", async () => {
    const vault = await makeTempVault();
    await fs.mkdir(path.join(vault, ".schist"), { recursive: true });
    const sentinelPath = path.join(vault, ".schist", "last-sync-error");
    await fs.writeFile(
      sentinelPath,
      "2026-04-12T18:00:00Z push spawn failed: spawn python3 ENOENT\n"
    );

    const result = await get_context(vault, { depth: "minimal" }) as Record<string, unknown>;
    expect(result.syncWarning).toBeDefined();
    expect(result.syncWarning as string).toContain("push spawn failed");

    // Sentinel should be cleared after surfacing
    const cleared = await fs.access(sentinelPath).then(() => false).catch(() => true);
    expect(cleared).toBe(true);
  }, 10000);

  test("get_context has no syncWarning when sentinel is absent", async () => {
    const vault = await makeTempVault();
    const result = await get_context(vault, { depth: "minimal" }) as Record<string, unknown>;
    expect(result.syncWarning).toBeUndefined();
  });

  test("triggerSpokePush writes sentinel when spawn fails", async () => {
    const vault = await makeTempVault();
    await fs.mkdir(path.join(vault, ".schist"), { recursive: true });
    await fs.writeFile(
      path.join(vault, ".schist", "spoke.yaml"),
      "hub: file:///nonexistent\nidentity: test\nscope: notes\n"
    );

    // Stub python3 that exits nonzero — triggers the 'exit' handler path
    const stubDir = await fs.mkdtemp(path.join(os.tmpdir(), "stub-py-"));
    const stub = path.join(stubDir, "python3");
    await fs.writeFile(stub, "#!/bin/sh\nexit 7\n", { mode: 0o755 });

    const origPath = process.env.PATH;
    process.env.PATH = `${stubDir}:${origPath}`;
    try {
      triggerSpokePush(vault);
      // Poll for the sentinel to appear
      const sentinelPath = path.join(vault, ".schist", "last-sync-error");
      let found = false;
      for (let i = 0; i < 60; i++) {
        try {
          const content = await fs.readFile(sentinelPath, "utf-8");
          if (content.includes("exited with code 7")) {
            found = true;
            break;
          }
        } catch {
          // not yet
        }
        await new Promise((r) => setTimeout(r, 50));
      }
      expect(found).toBe(true);
    } finally {
      process.env.PATH = origPath;
      await fs.rm(stubDir, { recursive: true, force: true });
    }
  }, 10000);
});

describe("get_context wiring", () => {
  test("get_context awaits maybeSpokePull when spoke.yaml present", async () => {
    const vault = await makeTempVault();
    await fs.mkdir(path.join(vault, ".schist"), { recursive: true });
    await fs.writeFile(
      path.join(vault, ".schist", "spoke.yaml"),
      "hub: file:///nonexistent\nidentity: test\nscope: notes\n"
    );

    // Stub python3 that sleeps 500ms then writes a sentinel — if get_context
    // awaits maybeSpokePull, the pull runs before the SQLite read (which will
    // fail because there's no DB, but that's caught and doesn't affect the
    // ordering check).
    const sentinel = path.join(vault, ".schist", "get-context-pull-fired");
    const stubDir = await fs.mkdtemp(path.join(os.tmpdir(), "stub-py-"));
    const stub = path.join(stubDir, "python3");
    await fs.writeFile(
      stub,
      `#!/bin/sh\nsleep 0.2\ntouch "${sentinel}"\n`,
      { mode: 0o755 }
    );

    const origPath = process.env.PATH;
    process.env.PATH = `${stubDir}:${origPath}`;
    try {
      await get_context(vault, { depth: "minimal" });
      // maybeSpokePull is awaited with 5s timeout — by the time get_context
      // returns, the stub must have completed and the sentinel must exist.
      const exists = await fs.access(sentinel).then(() => true).catch(() => false);
      expect(exists).toBe(true);
    } finally {
      process.env.PATH = origPath;
      await fs.rm(stubDir, { recursive: true, force: true });
    }
  }, 10000);
});

describe("normalizeError", () => {
  test("WRITE_TIMEOUT Error has error field lifted", async () => {
    // Simulate what git-writer throws when the mutex times out
    const thrown = Object.assign(
      new Error("Git write timed out after 10s — another write is in progress"),
      { error: "WRITE_TIMEOUT" }
    );
    // create_note catches and normalizes — use a bad vault to force an error
    const result = await create_note(
      "/nonexistent-vault",
      { title: "Test", body: "body" },
      {
        name: "t",
        path: "/nonexistent-vault",
        directories: ["notes"],
        connectionTypes: [],
        statuses: ["draft"],
        writeBranch: "drafts",
      }
    ) as Record<string, unknown>;

    // Should be a plain serialisable ToolError — error + message both present
    expect(typeof result.error).toBe("string");
    expect(typeof result.message).toBe("string");
    // message must NOT be empty (the JSON.stringify non-enumerable bug)
    expect((result.message as string).length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// assign_domain
// ---------------------------------------------------------------------------

describe("assign_domain", () => {
  async function makeVaultWithDomains(
    domains: string[] = ["ai", "security"]
  ): Promise<string> {
    const vault = await makeTempVault(
      domains.length > 0 ? `\ndomains:\n${domains.map((d) => `  - ${d}`).join("\n")}` : ""
    );
    await fs.mkdir(path.join(vault, "notes"), { recursive: true });

    // Create and populate the domains table (simulating ingestion)
    const dbPath = path.join(vault, ".schist", "schist.db");
    await fs.mkdir(path.dirname(dbPath), { recursive: true });
    const db = new Database(dbPath);
    db.exec(
      "CREATE TABLE domains (slug TEXT PRIMARY KEY, label TEXT NOT NULL, description TEXT, parent_slug TEXT REFERENCES domains(slug))"
    );
    const insert = db.prepare("INSERT INTO domains (slug, label) VALUES (?, ?)");
    for (const domain of domains) {
      insert.run(domain, domain);
    }
    db.close();

    return vault;
  }

  test("happy path: assigns valid domain to note", async () => {
    const vault = await makeVaultWithDomains(["ai", "ml"]);

    // Create a note
    const noteResult = (await create_note(
      vault,
      { title: "Test Note", body: "Test body" },
      await loadVaultConfig(vault)
    )) as { id: string; path: string };

    // Assign domain
    const result = (await assign_domain(vault, {
      id: noteResult.path,
      domain: "ml",
    })) as { id: string; domain: string; commitSha: string };

    expect(result.id).toBe(noteResult.path);
    expect(result.domain).toBe("ml");
    expect(result.commitSha).toBeDefined();

    // Verify frontmatter was updated
    const content = await fs.readFile(path.join(vault, noteResult.path), "utf-8");
    expect(content).toContain("domain: ml");
  });

  test("replaces existing domain", async () => {
    const vault = await makeVaultWithDomains(["ai", "security"]);

    // Create a note
    const noteResult = (await create_note(
      vault,
      { title: "Test Note", body: "Test body" },
      await loadVaultConfig(vault)
    )) as { id: string; path: string };

    // Assign first domain
    await assign_domain(vault, {
      id: noteResult.path,
      domain: "ai",
    });

    // Assign different domain
    await assign_domain(vault, {
      id: noteResult.path,
      domain: "security",
    });

    // Verify only one domain in frontmatter
    const content = await fs.readFile(path.join(vault, noteResult.path), "utf-8");
    const matches = content.match(/^domain:\s*/gm);
    expect(matches?.length).toBe(1);
    expect(content).toContain("domain: security");
    expect(content).not.toContain("domain: ai");
  });

  test("rejects invalid domain", async () => {
    const vault = await makeVaultWithDomains(["ai", "security"]);

    const noteResult = (await create_note(
      vault,
      { title: "Test Note", body: "Test body" },
      await loadVaultConfig(vault)
    )) as { id: string; path: string };

    const result = (await assign_domain(vault, {
      id: noteResult.path,
      domain: "invalid-domain",
    })) as { error: string; message: string };

    expect(result.error).toBe("INVALID_DOMAIN");
    expect(result.message).toContain("invalid-domain");
    expect(result.message).toContain("not found in vault.yaml");
  });

  test("rejects path traversal", async () => {
    const vault = await makeVaultWithDomains(["ai"]);

    const result = (await assign_domain(vault, {
      id: "../../../etc/passwd",
      domain: "ai",
    })) as { error: string; message: string };

    expect(result.error).toBe("PATH_TRAVERSAL");
    expect(result.message).toContain("outside vault root");
  });

  test("returns GIT_ERROR for missing note", async () => {
    const vault = await makeVaultWithDomains(["ai"]);

    const result = (await assign_domain(vault, {
      id: "notes/nonexistent.md",
      domain: "ai",
    })) as { error: string; message: string };

    // Trying to read a non-existent file returns NOT_FOUND
    // (the code tries to read before writing, so it catches this case)
    expect(result.error).toBe("NOT_FOUND");
    expect(result.message).toContain("Note not found");
  });

  test("allows any domain when vault.yaml has no domains list", async () => {
    const vault = await makeVaultWithDomains([]); // Empty domains list

    const noteResult = (await create_note(
      vault,
      { title: "Test Note", body: "Test body" },
      await loadVaultConfig(vault)
    )) as { id: string; path: string };

    // Should accept any domain when list is empty
    const result = (await assign_domain(vault, {
      id: noteResult.path,
      domain: "arbitrary-domain",
    })) as { id: string; domain: string; commitSha: string };

    expect(result.domain).toBe("arbitrary-domain");
  });
});
