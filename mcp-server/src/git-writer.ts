import { execFile as execFileCb } from "child_process";
import { promisify } from "util";
import { Mutex, withTimeout } from "async-mutex";
import * as fs from "fs/promises";
import * as path from "path";

const execFile = promisify(execFileCb);
const WRITE_TIMEOUT_ERROR = Object.assign(
  new Error("Git write timed out after 10s — another write is in progress"),
  { error: "WRITE_TIMEOUT" }
);

/** @internal — exported for test instrumentation only */
export const writeMutex = withTimeout(new Mutex(), 10000, WRITE_TIMEOUT_ERROR);

async function git(vaultRoot: string, args: string[]): Promise<string> {
  const { stdout } = await execFile("git", args, { cwd: vaultRoot });
  return stdout.trim();
}

async function getWriteBranch(vaultRoot: string): Promise<string> {
  try {
    const configPath = path.join(vaultRoot, "schist.yaml");
    const content = await fs.readFile(configPath, "utf-8");
    const match = content.match(/write_branch:\s*["']?(\S+?)["']?\s*$/m);
    return match ? match[1] : "drafts";
  } catch {
    return "drafts";
  }
}

async function ensureBranch(vaultRoot: string, branch: string): Promise<void> {
  try {
    await git(vaultRoot, ["rev-parse", "--verify", branch]);
  } catch {
    await git(vaultRoot, ["branch", branch]);
  }
}

function assertPathSafe(vaultRoot: string, relPath: string): void {
  const absVaultRoot = path.resolve(vaultRoot);
  const absPath = path.resolve(vaultRoot, relPath);
  if (!absPath.startsWith(absVaultRoot + path.sep) && absPath !== absVaultRoot) {
    throw { error: "PATH_TRAVERSAL", message: "Write path is outside vault root" };
  }
}

/**
 * Acquires the write mutex, checks out the write branch, runs fn(absPath),
 * then stages + commits relPath. Shared by writeNote and appendToNote to
 * eliminate duplicated mutex/git boilerplate.
 */
async function withWriteLock(
  vaultRoot: string,
  relPath: string,
  commitMessage: string,
  fn: (absPath: string) => Promise<void>
): Promise<{ path: string; commitSha: string }> {
  const release = await writeMutex.acquire();
  try {
    const branch = await getWriteBranch(vaultRoot);
    await ensureBranch(vaultRoot, branch);
    await git(vaultRoot, ["checkout", branch]);

    const absPath = path.resolve(vaultRoot, relPath);
    await fs.mkdir(path.dirname(absPath), { recursive: true });
    await fn(absPath);

    await git(vaultRoot, ["add", relPath]);
    // NEVER --no-verify — hard coded out
    await git(vaultRoot, ["commit", "-m", commitMessage]);
    const sha = await git(vaultRoot, ["rev-parse", "HEAD"]);
    return { path: relPath, commitSha: sha };
  } finally {
    release();
  }
}

export async function writeNote(
  vaultRoot: string,
  relPath: string,
  content: string
): Promise<{ path: string; commitSha: string }> {
  assertPathSafe(vaultRoot, relPath);
  const titleMatch = content.match(/^title:\s*["']?(.+?)["']?\s*$/m);
  const title = titleMatch ? titleMatch[1] : relPath;
  return withWriteLock(
    vaultRoot,
    relPath,
    `feat(schist): write ${title} — via MCP`,
    async (absPath) => {
      await fs.writeFile(absPath, content, "utf-8");
    }
  );
}

export async function appendToNote(
  vaultRoot: string,
  relPath: string,
  addition: string
): Promise<{ path: string; commitSha: string }> {
  assertPathSafe(vaultRoot, relPath);
  return withWriteLock(
    vaultRoot,
    relPath,
    `feat(schist): append to ${relPath} — via MCP`,
    async (absPath) => {
      let existing = "";
      try {
        existing = await fs.readFile(absPath, "utf-8");
      } catch {
        // file doesn't exist yet
      }
      const newContent = existing + (existing.endsWith("\n") ? "" : "\n") + addition;
      await fs.writeFile(absPath, newContent, "utf-8");
    }
  );
}
