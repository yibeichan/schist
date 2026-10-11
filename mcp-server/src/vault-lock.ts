import { spawn, type ChildProcess } from "child_process";

/**
 * Cross-process vault write lock (#724). Node has no portable flock, so the
 * lock is held by a `schist lock hold` child for the span of one write: the
 * CLI and every MCP server take the same flock on <vault>/.schist/write.lock.
 * Closing the child's stdin releases it; so does this process dying (EOF) or
 * the child dying (the kernel or the NFS server drops the lock).
 */
export interface VaultLockHandle { assertStillHeld(): void; readonly crossProcess: boolean }

export class VaultLockTimeout extends Error {
  constructor(public holder: string) { super(`vault write lock busy (held by ${holder})`); }
}

const EXIT_TIMEOUT = 75;
const GIT_COMMIT_TIMEOUT_MS = Number(process.env.SCHIST_GIT_COMMIT_TIMEOUT_MS) || 120000;
// Covers a slow post-commit ingest: the commit is the last step under the lock.
const MAX_HOLD_S = Math.ceil(GIT_COMMIT_TIMEOUT_MS / 1000) + 60;
// How long release waits for a helper that ignores stdin EOF before SIGKILL.
const RELEASE_GRACE_MS = 5000;

let warned = false;
export function _resetVaultLockWarningForTests(): void { warned = false; }

function lockBin(): string {
  return process.env.SCHIST_LOCK_BIN?.trim() || process.env.SCHIST_BIN?.trim() || "schist";
}

function warnOnce(reason: string): void {
  if (warned) return;
  warned = true;
  console.error(`[schist] cross-process vault lock unavailable: ${reason}; writes are serialized within this process only`);
}

type Acquired = { child: ChildProcess; exited: () => string | null };

// Node does not guarantee stdout/stderr are drained when `exit` fires, so the
// helper's final output (TIMEOUT holder, late EXPIRED, stderr) is read on
// `close`. `exit` only marks the helper dead so assertStillHeld reacts at once.
const ACQUIRE_GRACE_MS = 5000;

function acquire(vaultRoot: string, timeoutMs: number): Promise<Acquired | { unavailable: string }> {
  return new Promise((resolve, reject) => {
    let child: ChildProcess;
    try {
      child = spawn(lockBin(), ["--vault", vaultRoot, "lock", "hold",
        "--timeout", String(timeoutMs / 1000), "--max-hold", String(MAX_HOLD_S)],
        { stdio: ["pipe", "pipe", "pipe"] });
    } catch (e) {
      resolve({ unavailable: String(e) });
      return;
    }
    let pending = "", out = "", err = "", settled = false;
    let expired = false, dead = false, spawnError: string | null = null;
    let deadWhy = "";
    const exited = (): string | null => {
      if (spawnError !== null) return spawnError;
      if (!dead) return null;
      return expired ? "max-hold expired" : deadWhy;
    };
    // Backstop: a helper that wedges before LOCKED or exit (slow `uv run`
    // resolution, an NFS stall) must not block the caller forever.
    const backstop = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill("SIGKILL");
      reject(new VaultLockTimeout("unknown"));
    }, timeoutMs + ACQUIRE_GRACE_MS);
    const settle = () => { settled = true; clearTimeout(backstop); };
    child.stdout!.setEncoding("utf-8");
    child.stderr!.setEncoding("utf-8").on("data", (d: string) => { err += d; });
    // Stdout is parsed by complete lines, so a marker split across chunks still matches.
    child.stdout!.on("data", (d: string) => {
      pending += d;
      let nl: number;
      while ((nl = pending.indexOf("\n")) >= 0) {
        const line = pending.slice(0, nl).trim();
        pending = pending.slice(nl + 1);
        out += line + "\n";
        if (line === "LOCKED" && !settled) {
          settle();
          resolve({ child, exited });
        } else if (line === "EXPIRED") {
          expired = true;
        }
      }
    });
    // A child can fail to spawn (ENOENT) after spawn() returns; that is
    // "unavailable", not an exception.
    child.on("error", (e) => {
      spawnError ??= String(e);
      if (!settled) { settle(); resolve({ unavailable: String(e) }); }
    });
    child.on("exit", (code, signal) => {
      dead = true;
      deadWhy = signal ? `killed by ${signal}` : `exit ${code}`;
    });
    child.on("close", (code, signal) => {
      dead = true;
      deadWhy ||= signal ? `killed by ${signal}` : `exit ${code}`;
      // Flush an unterminated last line so it is not lost.
      const last = pending.trim();
      pending = "";
      if (last) out += last + "\n";
      if (last === "EXPIRED") expired = true;
      if (settled) return;
      settle();
      if (code === EXIT_TIMEOUT) {
        const m = /TIMEOUT holder=(\S+)/.exec(out);
        reject(new VaultLockTimeout(m ? m[1] : "unknown"));
      } else {
        resolve({ unavailable: err.trim().split("\n").pop() || `helper exit ${code}` });
      }
    });
  });
}

export async function withVaultLock<T>(
  vaultRoot: string,
  timeoutMs: number,
  fn: (h: VaultLockHandle) => Promise<T>,
): Promise<T> {
  const got = await acquire(vaultRoot, timeoutMs);
  if ("unavailable" in got) {
    warnOnce(got.unavailable);
    return fn({ crossProcess: false, assertStillHeld() {} });
  }
  const handle: VaultLockHandle = {
    crossProcess: true,
    assertStillHeld() {
      const why = got.exited();
      if (why !== null) throw { error: "GIT_ERROR", message: `vault write lock lost (helper exited: ${why})` };
    },
  };
  // Test-only access to the helper process; hidden from enumeration.
  Object.defineProperty(handle, "_child", { value: got.child, enumerable: false });
  try {
    return await fn(handle);
  } finally {
    await release(got);
  }
}

async function release(got: Acquired): Promise<void> {
  const child = got.child;
  if (child.exitCode !== null || child.signalCode !== null) return;
  const done = new Promise<void>((r) => child.once("close", () => r()));
  child.stdin!.on("error", () => {});
  child.stdin!.end();
  let timer: NodeJS.Timeout | undefined;
  const timedOut = new Promise<boolean>((r) => { timer = setTimeout(() => r(true), RELEASE_GRACE_MS); });
  const hung = await Promise.race([done.then(() => false), timedOut]);
  clearTimeout(timer);
  if (hung) {
    child.kill("SIGKILL");
    await Promise.race([done, new Promise<void>((r) => setTimeout(r, 1000))]);
  }
}
