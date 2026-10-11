import { recentAddedPaths } from "../src/tools.js";
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import { execFile as execFileCb } from "child_process";
import { promisify } from "util";

const execFile = promisify(execFileCb);
const dirs: string[] = [];

async function vaultWith(files: string[][]): Promise<{ vault: string; shas: string[] }> {
  const vault = await fs.mkdtemp(path.join(os.tmpdir(), "schist-recent-"));
  dirs.push(vault);
  await execFile("git", ["init", "-q"], { cwd: vault });
  await execFile("git", ["config", "user.email", "t@example.com"], { cwd: vault });
  await execFile("git", ["config", "user.name", "t"], { cwd: vault });
  const shas: string[] = [];
  for (const commit of files) {
    for (const rel of commit) {
      await fs.mkdir(path.dirname(path.join(vault, rel)), { recursive: true });
      await fs.writeFile(path.join(vault, rel), `${rel}\n`);
    }
    await execFile("git", ["add", "-A"], { cwd: vault });
    await execFile("git", ["commit", "-qm", "add"], { cwd: vault });
    shas.push((await execFile("git", ["rev-parse", "--short", "HEAD"], { cwd: vault })).stdout.trim());
  }
  return { vault, shas };
}

describe("recentAddedPaths (#699)", () => {
  afterAll(async () => {
    for (const d of dirs) await fs.rm(d, { recursive: true, force: true });
  });

  test("non-ASCII names come back verbatim, not as quoted octal, and match the scope", async () => {
    const { vault } = await vaultWith([["research/café.md", "notes/naïve résumé.md"]]);

    const scoped = await recentAddedPaths(vault, ["research"]);
    expect(scoped.ok).toBe(true);
    expect(scoped.rows.map((r) => r.path)).toEqual(["research/café.md"]);

    const all = await recentAddedPaths(vault, []);
    expect(all.rows.map((r) => r.path).sort()).toEqual(["notes/naïve résumé.md", "research/café.md"]);
  });

  test("names git would quote (quotes, tabs, backslashes) and leading spaces survive", async () => {
    const odd = ['notes/say "hi".md', "notes/tab\there.md", "notes/back\\slash.md", "notes/ lead.md"];
    const { vault } = await vaultWith([odd]);

    const rows = (await recentAddedPaths(vault, ["notes"])).rows.map((r) => r.path).sort();
    expect(rows).toEqual([...odd].sort());
  });

  test("each path carries the commit that added it", async () => {
    const { vault, shas } = await vaultWith([["notes/first.md"], ["notes/second.md"]]);

    const rows = (await recentAddedPaths(vault, [])).rows;
    expect(rows).toEqual([
      { path: "notes/second.md", commit: shas[1] },
      { path: "notes/first.md", commit: shas[0] },
    ]);
  });

  test("stops at eight rows", async () => {
    const { vault } = await vaultWith([Array.from({ length: 12 }, (_, i) => `notes/n${i}.md`)]);
    expect((await recentAddedPaths(vault, [])).rows).toHaveLength(8);
  });
});
