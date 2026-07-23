import { afterAll, afterEach, describe, expect, jest, test } from "@jest/globals";
import { execFile as execFileCb } from "child_process";
import * as realFs from "fs/promises";
import * as os from "os";
import * as path from "path";
import { promisify } from "util";

const execFile = promisify(execFileCb);
const createdDirs = new Set<string>();
let statErrorPath: string | undefined;

jest.unstable_mockModule("fs/promises", () => ({
  ...realFs,
  stat: async (...args: Parameters<typeof realFs.stat>) => {
    const target = path.resolve(String(args[0]));
    if (target === statErrorPath) {
      throw Object.assign(new Error("injected stat failure"), { code: "EIO" });
    }
    return realFs.stat(...args);
  },
}));

const { writeNote } = await import("../src/git-writer.js");

async function makeTempVault(): Promise<string> {
  const dir = await realFs.mkdtemp(path.join(os.tmpdir(), "schist-stat-error-test-"));
  createdDirs.add(dir);
  await execFile("git", ["init"], { cwd: dir });
  await execFile("git", ["config", "user.email", "test@test.com"], { cwd: dir });
  await execFile("git", ["config", "user.name", "Test"], { cwd: dir });
  await realFs.writeFile(
    path.join(dir, "schist.yaml"),
    "name: test\nwrite_branch: drafts\n",
  );
  await execFile("git", ["add", "."], { cwd: dir });
  await execFile("git", ["commit", "-m", "init"], { cwd: dir });
  return dir;
}

afterEach(() => {
  statErrorPath = undefined;
});

afterAll(async () => {
  for (const dir of createdDirs) {
    await realFs.rm(dir, { recursive: true, force: true });
  }
});

describe("atomicWriteFile target mode errors (#443)", () => {
  test("a non-ENOENT stat error aborts without replacing the existing note", async () => {
    const vault = await makeTempVault();
    const relPath = "notes/existing.md";
    const absPath = path.join(vault, relPath);
    const original = "---\ntitle: Existing\n---\noriginal\n";

    await writeNote(vault, relPath, original);
    statErrorPath = absPath;

    await expect(
      writeNote(vault, relPath, "---\ntitle: Existing\n---\nreplacement\n"),
    ).rejects.toMatchObject({ code: "EIO" });

    expect(await realFs.readFile(absPath, "utf-8")).toBe(original);
    const stagedTemps = await realFs.readdir(path.join(vault, ".schist", "tmp"));
    expect(stagedTemps.filter((name) => name.endsWith(".tmp"))).toEqual([]);
    const { stdout: status } = await execFile("git", ["status", "--porcelain"], { cwd: vault });
    expect(status).toBe("");
  });
});
