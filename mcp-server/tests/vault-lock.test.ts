import { jest } from "@jest/globals";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { fileURLToPath } from "url";
import { withVaultLock, VaultLockTimeout, _resetVaultLockWarningForTests } from "../src/vault-lock.js";
import { localSchistWrapperScript, spawnHolder, tryLock } from "./local-schist-bin.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..", "..");
let tmp: string;
let vault: string;
let wrapper: string;
const prevBin = process.env.SCHIST_LOCK_BIN;

function writeScript(name: string, body: string): string {
  const p = path.join(tmp, name);
  fs.writeFileSync(p, body, { mode: 0o755 });
  return p;
}

beforeAll(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "vault-lock-"));
  vault = path.join(tmp, "vault");
  fs.mkdirSync(path.join(vault, ".schist"), { recursive: true });
  wrapper = writeScript("schist", localSchistWrapperScript(repoRoot));
  process.env.SCHIST_LOCK_BIN = wrapper;
});

afterAll(() => {
  if (prevBin === undefined) delete process.env.SCHIST_LOCK_BIN;
  else process.env.SCHIST_LOCK_BIN = prevBin;
  fs.rmSync(tmp, { recursive: true, force: true });
});

const probe = () => tryLock(wrapper, vault, 0.2);

test("holds the flock for the span of fn and releases after", async () => {
  await withVaultLock(vault, 5000, async (h) => {
    expect(h.crossProcess).toBe(true);
    expect(await probe()).toBe(75);
  });
  expect(await probe()).toBe(0);
}, 30000);

test("releases when fn throws", async () => {
  await expect(withVaultLock(vault, 5000, async () => { throw new Error("boom"); })).rejects.toThrow("boom");
  expect(await probe()).toBe(0);
}, 30000);

test("timeout names the holder", async () => {
  const holder = await spawnHolder(wrapper, vault);
  try {
    const err = await withVaultLock(vault, 500, async () => {}).catch((e) => e);
    expect(err).toBeInstanceOf(VaultLockTimeout);
    expect(err.message).toMatch(/^vault write lock busy \(held by .+\)$/);
    expect(err.holder).not.toBe("unknown");
  } finally { holder.stdin.end(); }
}, 30000);

test("lost lock: assertStillHeld throws after the helper dies", async () => {
  await withVaultLock(vault, 5000, async (h) => {
    expect(() => h.assertStillHeld()).not.toThrow();
    (h as any)._child.kill("SIGKILL");
    await new Promise((r) => setTimeout(r, 300));
    expect(() => h.assertStillHeld()).toThrow(/vault write lock lost/);
    try { h.assertStillHeld(); } catch (e: any) { expect(e.error).toBe("GIT_ERROR"); }
  });
}, 30000);

test("_child is not enumerable", async () => {
  await withVaultLock(vault, 5000, async (h) => {
    expect(Object.keys(h)).not.toContain("_child");
  });
}, 30000);

test("fallback when the CLI is missing: runs fn, warns once", async () => {
  _resetVaultLockWarningForTests();
  const prev = process.env.SCHIST_LOCK_BIN;
  process.env.SCHIST_LOCK_BIN = "/nonexistent/schist";
  const err = jest.spyOn(console, "error").mockImplementation(() => {});
  try {
    for (let i = 0; i < 2; i++) await withVaultLock(vault, 1000, async (h) => { expect(h.crossProcess).toBe(false); h.assertStillHeld(); });
    expect(err.mock.calls.filter((c) => String(c[0]).includes("cross-process vault lock unavailable")).length).toBe(1);
  } finally { process.env.SCHIST_LOCK_BIN = prev; err.mockRestore(); }
});

test("fallback when the CLI predates `lock` (usage error exit 2)", async () => {
  _resetVaultLockWarningForTests();
  const old = writeScript("old-schist", "#!/bin/sh\necho \"schist: error: argument command: invalid choice: 'lock'\" >&2; exit 2\n");
  const prev = process.env.SCHIST_LOCK_BIN;
  process.env.SCHIST_LOCK_BIN = old;
  const err = jest.spyOn(console, "error").mockImplementation(() => {});
  try {
    await withVaultLock(vault, 1000, async (h) => { expect(h.crossProcess).toBe(false); });
  } finally { process.env.SCHIST_LOCK_BIN = prev; err.mockRestore(); }
});

test("finally does not hang when the helper ignores stdin EOF", async () => {
  const stubborn = writeScript("stubborn", "#!/bin/sh\necho LOCKED\nexec sleep 60\n");
  const prev = process.env.SCHIST_LOCK_BIN;
  process.env.SCHIST_LOCK_BIN = stubborn;
  try {
    const t0 = Date.now();
    await withVaultLock(vault, 1000, async (h) => { expect(h.crossProcess).toBe(true); });
    expect(Date.now() - t0).toBeLessThan(15000);
  } finally { process.env.SCHIST_LOCK_BIN = prev; }
}, 30000);

test("EXPIRED is detected even when split across chunks", async () => {
  const slow = writeScript("slow", "#!/bin/sh\necho LOCKED\nprintf 'EXPI'\nsleep 0.2\necho 'RED'\nexit 76\n");
  const prev = process.env.SCHIST_LOCK_BIN;
  process.env.SCHIST_LOCK_BIN = slow;
  try {
    await withVaultLock(vault, 1000, async (h) => {
      await new Promise((r) => setTimeout(r, 600));
      expect(() => h.assertStillHeld()).toThrow(/max-hold expired/);
    });
  } finally { process.env.SCHIST_LOCK_BIN = prev; }
}, 30000);
