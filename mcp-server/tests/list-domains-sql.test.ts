import { listDomains } from "../src/sqlite-reader.js";
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import Database from "better-sqlite3";

async function makeDomainVault(
  domains: Array<{ slug: string; label: string; parent_slug?: string }>
): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-domains-test-"));
  const dbDir = path.join(dir, ".schist");
  await fs.mkdir(dbDir, { recursive: true });
  const dbPath = path.join(dbDir, "schist.db");

  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE domains (
      slug        TEXT PRIMARY KEY,
      label       TEXT NOT NULL,
      description TEXT,
      parent_slug TEXT REFERENCES domains(slug)
    );
  `);

  const insert = db.prepare(
    `INSERT INTO domains (slug, label, parent_slug) VALUES (?, ?, ?)`
  );
  for (const d of domains) {
    insert.run(d.slug, d.label, d.parent_slug ?? null);
  }
  db.close();

  return dir;
}

describe("listDomains — limit + offset pagination", () => {
  it("default limit 100: no opts returns first 100 of 120 rows", async () => {
    const domains = Array.from({ length: 120 }, (_, i) => ({
      slug: `domain-${String(i).padStart(3, "0")}`,
      label: `Domain ${i}`,
    }));
    const vault = await makeDomainVault(domains);
    const results = listDomains(vault);
    expect(results.length).toBe(100);
  });

  it("offset pagination: two pages of 100 cover all 120, no overlap", async () => {
    const domains = Array.from({ length: 120 }, (_, i) => ({
      slug: `domain-${String(i).padStart(3, "0")}`,
      label: `Domain ${i}`,
    }));
    const vault = await makeDomainVault(domains);

    const page1 = listDomains(vault, { limit: 100 });
    const page2 = listDomains(vault, { limit: 100, offset: 100 });

    expect(page1.length).toBe(100);
    expect(page2.length).toBe(20);

    const slugs1 = new Set(page1.map((r) => r.slug));
    const slugs2 = new Set(page2.map((r) => r.slug));
    for (const s of slugs1) expect(slugs2.has(s)).toBe(false);

    const allSlugs = new Set([...slugs1, ...slugs2]);
    expect(allSlugs.size).toBe(120);
  });

  it("smaller limit honored: limit 5 returns 5 rows in slug order", async () => {
    const domains = Array.from({ length: 20 }, (_, i) => ({
      slug: `dom-${String(i).padStart(2, "0")}`,
      label: `Dom ${i}`,
    }));
    const vault = await makeDomainVault(domains);

    const results = listDomains(vault, { limit: 5 });
    expect(results.length).toBe(5);
    expect(results[0].slug).toBe("dom-00");
    expect(results[4].slug).toBe("dom-04");
  });

  it("offset 0 default: limit 10 equals limit 10 offset 0", async () => {
    const domains = Array.from({ length: 20 }, (_, i) => ({
      slug: `x-${String(i).padStart(2, "0")}`,
      label: `X ${i}`,
    }));
    const vault = await makeDomainVault(domains);

    const a = listDomains(vault, { limit: 10 });
    const b = listDomains(vault, { limit: 10, offset: 0 });
    expect(a.map((r) => r.slug)).toEqual(b.map((r) => r.slug));
  });
});
