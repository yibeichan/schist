import { writeNote, deleteNote } from "../src/git-writer.js";
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import { execFile as execFileCb } from "child_process";
import { promisify } from "util";

const execFile = promisify(execFileCb);
const createdDirs = new Set<string>();

async function makeTempVault(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-concurrent-"));
  createdDirs.add(dir);
  await execFile("git", ["init"], { cwd: dir });
  await execFile("git", ["config", "user.email", "test@test.com"], { cwd: dir });
  await execFile("git", ["config", "user.name", "Test"], { cwd: dir });
  await fs.writeFile(path.join(dir, "schist.yaml"), "name: test\nwrite_branch: drafts\n");
  await execFile("git", ["add", "."], { cwd: dir });
  await execFile("git", ["commit", "-m", "init"], { cwd: dir });
  return dir;
}

async function preCommitHook(vault: string, body: string): Promise<void> {
  const hooks = (await execFile("git", ["rev-parse", "--git-path", "hooks"], { cwd: vault })).stdout.trim();
  const dir = path.isAbsolute(hooks) ? hooks : path.join(vault, hooks);
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(path.join(dir, "pre-commit"), `#!/bin/sh\n${body}\n`, { mode: 0o755 });
}

// Another writer's commit lands in the window between our `git add` and our
// `git commit`, sweeping everything staged (what a background `schist sync
// push` does), so our own commit then fails. Injected from pre-commit, the
// last point before our commit: commit the current index via plumbing, then
// fail our commit.
const SWEEP_STAGED_THEN_FAIL =
  'c=$(git commit-tree "$(git write-tree)" -p HEAD -m "sync(other): 1 file") && git update-ref HEAD "$c"; exit 1';
// HEAD moves, but to a commit that does NOT carry what we staged.
const UNRELATED_COMMIT_THEN_FAIL =
  'c=$(git commit-tree "HEAD^{tree}" -p HEAD -m "unrelated") && git update-ref HEAD "$c"; exit 1';

async function blobInHead(vault: string, rel: string): Promise<string | null> {
  try {
    return (await execFile("git", ["show", `HEAD:${rel}`], { cwd: vault })).stdout;
  } catch {
    return null;
  }
}

describe("git-writer: a concurrent commit of our staged paths", () => {
  afterAll(async () => {
    for (const dir of createdDirs) await fs.rm(dir, { recursive: true, force: true });
  });

  test("writeNote reports a write another commit already landed as committed", async () => {
    const vault = await makeTempVault();
    await writeNote(vault, "notes/seed.md", "---\ntitle: Seed\n---\nseed");
    await preCommitHook(vault, SWEEP_STAGED_THEN_FAIL);

    const content = "---\ntitle: Raced\n---\nbody";
    const result = await writeNote(vault, "notes/raced.md", content);

    expect(result.committed).toBe(true);
    expect(result.commitWarning).toContain("concurrent");
    expect(await blobInHead(vault, "notes/raced.md")).toBe(content);
    const head = (await execFile("git", ["rev-parse", "HEAD"], { cwd: vault })).stdout.trim();
    expect(result.commitSha).toBe(head);
  }, 30000);

  test("writeNote still fails when the commit genuinely did not land", async () => {
    const vault = await makeTempVault();
    await writeNote(vault, "notes/seed.md", "---\ntitle: Seed\n---\nseed");
    await preCommitHook(vault, "exit 1");

    await expect(writeNote(vault, "notes/refused.md", "---\ntitle: R\n---\nr")).rejects.toBeDefined();
    expect(await blobInHead(vault, "notes/refused.md")).toBeNull();
  }, 30000);

  test("HEAD moving is not enough: our content must be what landed", async () => {
    const vault = await makeTempVault();
    await writeNote(vault, "notes/seed.md", "---\ntitle: Seed\n---\nseed");
    await preCommitHook(vault, UNRELATED_COMMIT_THEN_FAIL);

    await expect(writeNote(vault, "notes/lost.md", "---\ntitle: L\n---\nl")).rejects.toBeDefined();
    expect(await blobInHead(vault, "notes/lost.md")).toBeNull();
  }, 30000);

  test("deleteNote reports a delete another commit already landed as committed", async () => {
    const vault = await makeTempVault();
    await writeNote(vault, "notes/doomed.md", "---\ntitle: Doomed\n---\nbye");
    await preCommitHook(vault, SWEEP_STAGED_THEN_FAIL);

    const result = await deleteNote(vault, "notes/doomed.md", "Doomed");

    expect(result.committed).toBe(true);
    expect(result.commitWarning).toContain("concurrent");
    expect(await blobInHead(vault, "notes/doomed.md")).toBeNull();
    // Not rolled back: the file stays deleted on disk too.
    await expect(fs.access(path.join(vault, "notes", "doomed.md"))).rejects.toBeDefined();
  }, 30000);
});
