import * as path from "path";
import { spawn, type ChildProcessWithoutNullStreams } from "child_process";

/**
 * Shell script for a test-local `schist` wrapper (SCHIST_LOCK_BIN) that runs
 * the repo's CLI as `python -m schist`. Same complete-venv probe as
 * local-ingest-bin.ts (an interpreter-existence check alone races uv's venv
 * creation).
 */
export function localSchistWrapperScript(repoRoot: string): string {
  const python = path.join(repoRoot, "cli", ".venv", "bin", "python");
  const cliDir = path.join(repoRoot, "cli");
  return [
    "#!/bin/sh",
    `if [ -x ${JSON.stringify(python)} ] && ${JSON.stringify(python)} -c 'import frontmatter' 2>/dev/null; then`,
    `  PYTHONPATH=${JSON.stringify(cliDir)} exec ${JSON.stringify(python)} -m schist "$@"`,
    "fi",
    `cd ${JSON.stringify(cliDir)} && exec uv run --with . python -m schist "$@"`,
    "",
  ].join("\n");
}

/** Spawn a raw `lock hold` and resolve once it prints LOCKED. End its stdin to release. */
export function spawnHolder(bin: string, vault: string): Promise<ChildProcessWithoutNullStreams> {
  return new Promise((resolve, reject) => {
    const child = spawn(bin, ["--vault", vault, "lock", "hold"], { stdio: ["pipe", "pipe", "pipe"] });
    let out = "";
    child.once("error", reject);
    child.once("exit", (code) => reject(new Error(`holder exited ${code} before LOCKED: ${out}`)));
    child.stdout.setEncoding("utf-8").on("data", (d: string) => {
      out += d;
      if (out.split("\n").includes("LOCKED")) {
        child.removeAllListeners("exit");
        resolve(child);
      }
    });
  });
}

/** Try to take the lock for up to timeoutS seconds; resolves the helper's exit code (0 = got it, 75 = busy). Its stdin is closed at once. */
export function tryLock(bin: string, vault: string, timeoutS: number): Promise<number | null> {
  return new Promise((resolve, reject) => {
    const child = spawn(bin, ["--vault", vault, "lock", "hold", "--timeout", String(timeoutS)], { stdio: ["pipe", "pipe", "pipe"] });
    child.once("error", reject);
    child.once("exit", (code) => resolve(code));
    child.stdin.end();
  });
}
