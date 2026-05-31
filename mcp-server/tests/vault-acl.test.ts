import { describe, expect, test, beforeEach, afterEach, afterAll, jest } from "@jest/globals";
import * as fs from "fs/promises";
import * as os from "os";
import { readdirSync, readFileSync as readFileSyncForFixtures } from "fs";
import { join as pathJoin, dirname as pathDirname } from "path";
import { fileURLToPath } from "url";
import { scopeMatches, canWrite, resolveAclIdentity, type VaultAcl, loadVaultAcl, deriveScope } from "../src/vault-acl.js";

describe("scopeMatches", () => {
  test("exact match returns true", () => {
    expect(scopeMatches(["notes"], "notes")).toBe(true);
  });
  test("wildcard matches anything", () => {
    expect(scopeMatches(["*"], "anything")).toBe(true);
    expect(scopeMatches(["*"], "")).toBe(true);
  });
  test("parent grants child via slash", () => {
    expect(scopeMatches(["projects"], "projects/foo")).toBe(true);
    expect(scopeMatches(["projects"], "projects/foo/bar")).toBe(true);
  });
  test("prefix without slash boundary does NOT match", () => {
    // 'research' does not grant 'researchx'
    expect(scopeMatches(["research"], "researchx")).toBe(false);
  });
  test("empty allowed array returns false", () => {
    expect(scopeMatches([], "notes")).toBe(false);
  });
  test("no match in non-empty list returns false", () => {
    expect(scopeMatches(["notes", "papers"], "logs")).toBe(false);
  });
});

describe("canWrite", () => {
  const acl: VaultAcl = {
    access: {
      alice: { read: ["*"], write: ["notes", "papers"] },
      admin: { read: ["*"], write: ["*"] },
    },
  };

  test("granted scope returns true", () => {
    expect(canWrite(acl, "alice", "notes")).toBe(true);
  });
  test("ungranted scope returns false", () => {
    expect(canWrite(acl, "alice", "logs")).toBe(false);
  });
  test("unknown identity returns false", () => {
    expect(canWrite(acl, "carol", "notes")).toBe(false);
  });
  test("wildcard write grants every scope", () => {
    expect(canWrite(acl, "admin", "anything")).toBe(true);
    expect(canWrite(acl, "admin", "")).toBe(true);
  });
});

describe("resolveAclIdentity", () => {
  // Mirrors cli/schist/pre_receive.py:resolve_identity precedence:
  // SCHIST_IDENTITY -> GL_USER -> fallback. Save/restore env so cases are
  // hermetic regardless of the ambient identity on a dev box.
  let savedIdentity: string | undefined;
  let savedGlUser: string | undefined;
  beforeEach(() => {
    savedIdentity = process.env.SCHIST_IDENTITY;
    savedGlUser = process.env.GL_USER;
    delete process.env.SCHIST_IDENTITY;
    delete process.env.GL_USER;
  });
  afterEach(() => {
    if (savedIdentity === undefined) delete process.env.SCHIST_IDENTITY;
    else process.env.SCHIST_IDENTITY = savedIdentity;
    if (savedGlUser === undefined) delete process.env.GL_USER;
    else process.env.GL_USER = savedGlUser;
  });

  test("prefers SCHIST_IDENTITY over GL_USER and fallback", () => {
    process.env.SCHIST_IDENTITY = "dragonfly";
    process.env.GL_USER = "gl-user";
    expect(resolveAclIdentity("claude-desktop")).toBe("dragonfly");
  });
  test("falls back to GL_USER when SCHIST_IDENTITY is unset", () => {
    process.env.GL_USER = "gl-user";
    expect(resolveAclIdentity("claude-desktop")).toBe("gl-user");
  });
  test("falls back to owner when neither env var is set", () => {
    expect(resolveAclIdentity("claude-desktop")).toBe("claude-desktop");
  });
  test("empty-string SCHIST_IDENTITY falls through (matches Python `or`)", () => {
    process.env.SCHIST_IDENTITY = "";
    expect(resolveAclIdentity("claude-desktop")).toBe("claude-desktop");
  });
});

const tmpDirs: string[] = [];
afterAll(async () => {
  for (const d of tmpDirs) {
    await fs.rm(d, { recursive: true, force: true });
  }
});

async function makeTempVault(vaultYaml: string | null): Promise<string> {
  const dir = await fs.mkdtemp(`${os.tmpdir()}/schist-acl-`);
  tmpDirs.push(dir);
  if (vaultYaml !== null) {
    await fs.writeFile(`${dir}/vault.yaml`, vaultYaml, "utf-8");
  }
  return dir;
}

describe("loadVaultAcl", () => {
  test("returns null when vault.yaml is missing", async () => {
    const dir = await makeTempVault(null);
    expect(loadVaultAcl(dir)).toBeNull();
  });

  test("returns null and logs warning on malformed YAML", async () => {
    const dir = await makeTempVault(":::not yaml:::");
    const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
    expect(loadVaultAcl(dir)).toBeNull();
    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  test("returns null when 'access' is missing", async () => {
    const dir = await makeTempVault("name: nope\n");
    const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
    expect(loadVaultAcl(dir)).toBeNull();
    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  test("parses a valid vault.yaml", async () => {
    const dir = await makeTempVault(`
vault_version: 1
name: t
scope_convention: flat
participants: [{name: alice}]
access:
  alice:
    read: ["*"]
    write: [notes, papers]
`);
    const acl = loadVaultAcl(dir);
    expect(acl).not.toBeNull();
    expect(acl!.access.alice.write).toEqual(["notes", "papers"]);
    expect(acl!.access.alice.read).toEqual(["*"]);
  });

  test("coerces non-string write entries defensively", async () => {
    // If vault.yaml has weird types, fall back to empty list rather than crash.
    const dir = await makeTempVault(`
access:
  alice:
    read: ["*"]
    write: [notes, 42, null]
`);
    const acl = loadVaultAcl(dir);
    expect(acl).not.toBeNull();
    expect(acl!.access.alice.write).toEqual(["notes", "42", ""]);
  });
});

describe("deriveScope", () => {
  test("top-level file under a directory returns the directory", () => {
    expect(deriveScope("notes/2026-05-28-foo.md")).toBe("notes");
  });
  test("nested directory returns the full parent path", () => {
    expect(deriveScope("projects/foo/2026-05-28-bar.md")).toBe("projects/foo");
  });
  test("deeply nested path", () => {
    expect(deriveScope("projects/foo/sub/2026-05-28-baz.md")).toBe("projects/foo/sub");
  });
  test("root-level file returns empty string", () => {
    expect(deriveScope("vault.yaml")).toBe("");
  });
  test("leading ./ is normalised away", () => {
    expect(deriveScope("./notes/foo.md")).toBe("notes");
  });
  test("trailing slash in input is normalised", () => {
    expect(deriveScope("notes/")).toBe("");
  });
});

// Fixtures live at <repo>/cli/schist/acl-fixtures/. The test file is at
// <repo>/mcp-server/tests/vault-acl.test.ts → walk up two directories.
const FIXTURES_DIR_TS = pathJoin(
  pathDirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "cli",
  "schist",
  "acl-fixtures",
);

interface ParityCase {
  identity: string;
  scope: string;
  canWrite: boolean;
}

describe("vault-acl parity fixtures", () => {
  const yamlFiles = readdirSync(FIXTURES_DIR_TS).filter((f) => f.endsWith(".yaml"));
  // Sanity-check: discover at least the four fixtures from Task 1.
  test("discovers parity fixtures", () => {
    expect(yamlFiles.length).toBeGreaterThanOrEqual(4);
  });

  for (const yamlFile of yamlFiles) {
    const base = yamlFile.replace(/\.yaml$/, "");
    test(`${base}: TS canWrite matches every case in ${base}.cases.json`, async () => {
      // Build a temp vault containing JUST this fixture as vault.yaml.
      const dir = await fs.mkdtemp(`${os.tmpdir()}/schist-parity-${base}-`);
      tmpDirs.push(dir);
      const yamlBody = readFileSyncForFixtures(pathJoin(FIXTURES_DIR_TS, yamlFile), "utf-8");
      await fs.writeFile(pathJoin(dir, "vault.yaml"), yamlBody, "utf-8");

      const acl = loadVaultAcl(dir);
      expect(acl).not.toBeNull();

      const cases: ParityCase[] = JSON.parse(
        readFileSyncForFixtures(pathJoin(FIXTURES_DIR_TS, `${base}.cases.json`), "utf-8"),
      );
      for (const c of cases) {
        expect({ ...c, actual: canWrite(acl!, c.identity, c.scope) }).toEqual({
          ...c,
          actual: c.canWrite,
        });
      }
    });
  }
});
