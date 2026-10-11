/**
 * Acceptance test for the cross-process vault write lock (#724).
 *
 * Two "server instances" plus a CLI writer commit into one vault at once.
 * Without the shared lock, one writer's `git commit` sweeps another's staged
 * file into the wrong commit (#419, #715).
 *
 * Each instance is its own copy of `git-writer.js`, made with
 * `jest.isolateModulesAsync`. Under this ts-jest ESM config that gives a
 * distinct module instance, hence a distinct in-process `writeMutex`, as two
 * MCP servers have. The first test pins that premise; if a Jest upgrade
 * stops isolating ESM imports it fails, and the fix is to run the second
 * instance in a spawned Node process on the built `dist/` instead.
 */
import { jest } from "@jest/globals";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { execFile, spawn } from "child_process";
import { promisify } from "util";
import { fileURLToPath } from "url";
import { localSchistWrapperScript } from "./local-schist-bin.js";

const execFileP = promisify(execFile);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const mcpRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(mcpRoot, "..");

let tmp: string;
let vault: string;
let wrapper: string;
const prevLockBin = process.env.SCHIST_LOCK_BIN;

const git = async (args: string[]) => (await execFileP("git", args, { cwd: vault })).stdout;

function run(bin: string, args: string[], env: NodeJS.ProcessEnv = {}): Promise<{ code: number | null; out: string }> {
  return new Promise((resolve, reject) => {
    const child = spawn(bin, args, { env: { ...process.env, ...env }, stdio: ["ignore", "pipe", "pipe"] });
    let out = "";
    child.stdout.on("data", (d) => (out += d));
    child.stderr.on("data", (d) => (out += d));
    child.once("error", reject);
    child.once("close", (code) => resolve({ code, out }));
  });
}

beforeAll(async () => {
  jest.setTimeout(240000);
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "vault-lock-acc-"));
  vault = path.join(tmp, "vault");
  fs.mkdirSync(vault, { recursive: true });
  wrapper = path.join(tmp, "schist");
  fs.writeFileSync(wrapper, localSchistWrapperScript(repoRoot), { mode: 0o755 });
  process.env.SCHIST_LOCK_BIN = process.env.SCHIST_LOCK_BIN ?? wrapper;
  await git(["init", "-q"]);
  await git(["config", "user.email", "t@example.com"]);
  await git(["config", "user.name", "T"]);
  fs.writeFileSync(path.join(vault, "schist.yaml"), "name: acc\nwrite_branch: drafts\n");
  fs.writeFileSync(path.join(vault, ".gitignore"), ".schist/\n");
  await git(["add", "."]);
  await git(["commit", "-q", "-m", "init"]);
}, 240000);

afterAll(() => {
  if (prevLockBin === undefined) delete process.env.SCHIST_LOCK_BIN;
  else process.env.SCHIST_LOCK_BIN = prevLockBin;
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("premise: isolateModulesAsync gives each instance its own writeMutex under this config", async () => {
  let a: typeof import("../src/git-writer.js") | undefined;
  let b: typeof import("../src/git-writer.js") | undefined;
  await jest.isolateModulesAsync(async () => { a = await import("../src/git-writer.js"); });
  await jest.isolateModulesAsync(async () => { b = await import("../src/git-writer.js"); });
  expect(a!.writeMutex).not.toBe(b!.writeMutex);
});

test("two server instances + a CLI writer: every commit lands under its own message", async () => {
  const N = 6;
  let a: typeof import("../src/git-writer.js") | undefined;
  let b: typeof import("../src/git-writer.js") | undefined;
  await jest.isolateModulesAsync(async () => { a = await import("../src/git-writer.js"); });
  await jest.isolateModulesAsync(async () => { b = await import("../src/git-writer.js"); });
  expect(a!.writeMutex).not.toBe(b!.writeMutex);

  const cli = run(wrapper, ["--vault", vault, "add", "--title", "cli note", "--dir", "notes", "--body", "from cli"]);
  const writes = Array.from({ length: N }, (_, i) =>
    (i % 2 ? a! : b!).writeNote(vault, `notes/n${i}.md`, `---\ntitle: n${i}\n---\nbody ${i}\n`, `n${i}`, "tester", { exclusive: true }));
  const results = await Promise.all(writes);
  const c = await cli;
  expect({ code: c.code, out: c.out }).toMatchObject({ code: 0 });
  results.forEach((r) => expect(r.committed).toBe(true));

  // Each of the N+1 newest commits touches exactly one file, and an
  // n<i> commit touches n<i>.md (a swept file would show two paths in one commit
  // and an empty or failed commit for the other).
  const raw = await git(["log", "--format=%x00%s", "--name-only", `-${N + 1}`]);
  const commits = raw.split("\0").filter(Boolean).map((blk) => {
    const [subject, ...files] = blk.split("\n").filter((l) => l.length > 0);
    return { subject, files };
  });
  expect(commits).toHaveLength(N + 1);
  commits.forEach((cm) => expect(cm.files).toHaveLength(1));
  for (let i = 0; i < N; i++) {
    const cm = commits.find((x) => x.files[0] === `notes/n${i}.md`);
    expect(cm).toBeDefined();
    expect(cm!.subject).toContain(`n${i}`);
  }
  expect(commits.filter((x) => !/^notes\/n\d\.md$/.test(x.files[0]))).toHaveLength(1);
  expect((await git(["status", "--porcelain"])).trim()).toBe("");
}, 240000);
