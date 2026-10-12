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

/**
 * The helper ran but could not take the lock (a mount without lock support,
 * a permission error, a crash). Not a fallback case: writing without the
 * lock is the corruption it exists to prevent, so the write fails instead.
 */
export class VaultLockFailed extends Error {
  constructor(public detail: string) { super(`vault write lock could not be taken: ${detail}`); }
}

const EXIT_TIMEOUT = 75;
// argparse's exit status for a usage error: the CLI predates `lock hold`.
const EXIT_USAGE = 2;
const GIT_COMMIT_TIMEOUT_MS = Number(process.env.SCHIST_GIT_COMMIT_TIMEOUT_MS) || 120000;
// A valve against a parent that is alive but hung; a parent that dies closes
// stdin and ends the hold at once. It has to outlast a whole write, whose git
// steps each have their own 30 s ceiling on a stalled NFS mount, so it is
// generous: an expiry mid-commit is reported, but it is still a lost lock.
const MAX_HOLD_S = Math.max(600, Math.ceil(GIT_COMMIT_TIMEOUT_MS / 1000) * 3);
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
      // detached: its own process group, so a kill reaches the Python helper
      // even when SCHIST_LOCK_BIN is a wrapper (a `uv run` shim) that does not
      // exec it. Killing only the wrapper would leave the helper holding the lock.
      child = spawn(lockBin(), ["--vault", vaultRoot, "lock", "hold",
        "--timeout", String(timeoutMs / 1000), "--max-hold", String(MAX_HOLD_S)],
        { stdio: ["pipe", "pipe", "pipe"], detached: true });
    } catch (e) {
      resolve({ unavailable: String(e) });
      return;
    }
    let pending = "", out = "", err = "", settled = false;
    let expired = false, dead = false, spawnError: string | null = null;
    let deadWhy = "";
    const exited = (): string | null => {
      if (spawnError !== null) return spawnError;
      // The helper prints EXPIRED as it gives the flock up, so the lock is
      // gone from that line on, even while the process is still exiting.
      if (expired) return "max-hold expired";
      if (!dead) return null;
      return deadWhy;
    };
    // Backstop: a helper that wedges before LOCKED or exit (slow `uv run`
    // resolution, an NFS stall) must not block the caller forever.
    const backstop = setTimeout(() => {
      if (settled) return;
      settled = true;
      endStdin(child);
      killGroup(child, "SIGKILL");
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
      if (settled) return;
      settle();
      // Only a missing binary means "no helper here"; any other spawn failure
      // (EACCES, EMFILE) is a lock that could not be taken.
      if ((e as NodeJS.ErrnoException).code === "ENOENT") resolve({ unavailable: String(e) });
      else reject(new VaultLockFailed(String(e)));
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
      const lastErr = err.trim().split("\n").pop() || "";
      if (code === EXIT_TIMEOUT) {
        // To the end of the line: an older CLI's holder note has spaces.
        const m = /^TIMEOUT holder=(.+)$/m.exec(out);
        reject(new VaultLockTimeout(m ? m[1].trim() : "unknown"));
      } else if (code === EXIT_USAGE && /invalid choice: 'lock'/.test(err)) {
        resolve({ unavailable: `this schist CLI predates the vault lock (${lastErr})` });
      } else {
        reject(new VaultLockFailed(lastErr || `helper ${signal ? `killed by ${signal}` : `exited ${code}`} before taking the lock`));
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

function endStdin(child: ChildProcess): void {
  child.stdin?.on("error", () => {});
  child.stdin?.end();
}

function killGroup(child: ChildProcess, sig: NodeJS.Signals): void {
  if (child.pid === undefined) return;
  try { process.kill(-child.pid, sig); } catch { /* already gone */ }
}

async function release(got: Acquired): Promise<void> {
  const child = got.child;
  if (child.exitCode !== null || child.signalCode !== null) return;
  const done = new Promise<void>((r) => child.once("close", () => r()));
  endStdin(child);
  let timer: NodeJS.Timeout | undefined;
  const timedOut = new Promise<boolean>((r) => { timer = setTimeout(() => r(true), RELEASE_GRACE_MS); });
  const hung = await Promise.race([done.then(() => false), timedOut]);
  clearTimeout(timer);
  if (hung) {
    killGroup(child, "SIGKILL");
    await Promise.race([done, new Promise<void>((r) => setTimeout(r, 1000))]);
  }
}
