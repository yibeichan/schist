import { describe, expect, test, afterAll, jest } from "@jest/globals";
import * as fs from "fs/promises";
import * as os from "os";
import { scopeMatches, canWrite, type VaultAcl, loadVaultAcl } from "../src/vault-acl.js";

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
