import { describe, expect, test } from "@jest/globals";
import { scopeMatches, canWrite, type VaultAcl } from "../src/vault-acl.js";

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
