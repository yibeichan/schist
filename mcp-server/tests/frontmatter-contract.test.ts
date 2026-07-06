/**
 * schema/frontmatter-contract.json conformance — TS side (#130 slice A).
 *
 * The frontmatter field lists used to live three times: create_note's written
 * metadata, update_note's PATCHABLE_FRONTMATTER_KEYS, and Python ingest's read
 * set — prose-only in schema/SCHEMA.md, with no machine check that they agree.
 * The contract JSON is the single source of truth; this suite pins the two TS
 * lists to it, and cli/tests/test_frontmatter_contract.py pins ingest's read
 * set. A field added on either side without updating the contract fails that
 * language's CI.
 *
 * Consumers must ignore descriptor keys they don't know — new keys may be
 * added to the contract without breaking either suite.
 */
import * as fs from "fs/promises";
import { readFileSync } from "node:fs";
import * as path from "path";
import * as os from "os";
import { execFile as execFileCb } from "child_process";
import { promisify } from "util";
import { fileURLToPath } from "url";
import { loadVaultConfig, create_note, update_note, PATCHABLE_FRONTMATTER_KEYS } from "../src/tools.js";
import { parseNote } from "../src/markdown-parser.js";

const execFile = promisify(execFileCb);

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type FieldDescriptor = {
  field: string;
  type: string;
  appliesTo: string[];
  writtenBy: string[];
  readBy: string[];
  invalid: string | null;
  indexColumn: string | null;
};

type ContractDocument = {
  schemaVersion: number;
  fields: FieldDescriptor[];
};

function loadContractDocument(): ContractDocument {
  const contractPath = path.resolve(__dirname, "..", "..", "schema", "frontmatter-contract.json");
  return JSON.parse(readFileSync(contractPath, "utf-8")) as ContractDocument;
}

function loadContract(): FieldDescriptor[] {
  return loadContractDocument().fields;
}

/** Sorted set-difference report — makes drift failures name the exact fields. */
function drift(actual: Set<string>, contract: Set<string>): {
  inContractButNotInCode: string[];
  inCodeButNotInContract: string[];
} {
  return {
    inContractButNotInCode: [...contract].filter((f) => !actual.has(f)).sort(),
    inCodeButNotInContract: [...actual].filter((f) => !contract.has(f)).sort(),
  };
}

const createdDirs = new Set<string>();
const envSnapshot: Record<string, string | undefined> = {};
const TEST_AGENT = "test-agent";

async function makeTempVault(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-fm-contract-"));
  createdDirs.add(dir);
  await execFile("git", ["init"], { cwd: dir });
  await execFile("git", ["config", "user.email", "test@test.com"], { cwd: dir });
  await execFile("git", ["config", "user.name", "Test"], { cwd: dir });
  await fs.writeFile(
    path.join(dir, "schist.yaml"),
    [
      "name: Contract Test Vault",
      "write_branch: drafts",
      "directories:",
      "  - notes",
      "statuses:",
      "  - draft",
      "connection_types:",
      "  - related",
      "",
    ].join("\n"),
  );
  await execFile("git", ["add", "."], { cwd: dir });
  await execFile("git", ["commit", "-m", "init"], { cwd: dir });
  return dir;
}

beforeAll(() => {
  envSnapshot.SCHIST_AGENT_ID = process.env.SCHIST_AGENT_ID;
  process.env.SCHIST_AGENT_ID = TEST_AGENT;
});
afterAll(async () => {
  if (envSnapshot.SCHIST_AGENT_ID === undefined) delete process.env.SCHIST_AGENT_ID;
  else process.env.SCHIST_AGENT_ID = envSnapshot.SCHIST_AGENT_ID;
  for (const dir of createdDirs) {
    await fs.rm(dir, { recursive: true, force: true });
  }
});

describe("frontmatter contract fixture", () => {
  const contract = loadContract();

  test("is present and non-trivial", () => {
    // An emptied/mangled fixture must fail loudly, not vacuously pass the
    // set-equality checks below (same guard as the slug-parity fixture).
    expect(loadContractDocument().schemaVersion).toBe(1);
    expect(contract.length).toBeGreaterThanOrEqual(20);
  });

  test("field names are unique", () => {
    const names = contract.map((d) => d.field);
    expect(names.length).toBe(new Set(names).size);
  });

  test("descriptors use only the known vocabulary", () => {
    // Typos in enum-like descriptor values would silently drop a field from
    // the filtered sets both suites assert against — fail them here instead.
    const APPLIES_TO = new Set(["documents", "concepts", "papers"]);
    // cli_add is documentation-only (see the contract's $comment): `schist add`
    // writes frontmatter with none of the MCP normalizations and has no
    // conformance suite of its own.
    const WRITTEN_BY = new Set(["create_note", "update_note", "cli_add"]);
    const READ_BY = new Set(["ingest", "parseNote"]);
    const INVALID = new Set([
      "coerce-null", "coerce-int-or-null", "stringify", "stringify-scalar",
      "drop-invalid-items", "fallback",
    ]);
    const violations: string[] = [];
    for (const d of contract) {
      for (const v of d.appliesTo.filter((v) => !APPLIES_TO.has(v))) {
        violations.push(`field '${d.field}': unknown appliesTo '${v}'`);
      }
      for (const v of d.writtenBy.filter((v) => !WRITTEN_BY.has(v))) {
        violations.push(`field '${d.field}': unknown writtenBy '${v}'`);
      }
      for (const v of d.readBy.filter((v) => !READ_BY.has(v))) {
        violations.push(`field '${d.field}': unknown readBy '${v}'`);
      }
      if (d.invalid !== null && !INVALID.has(d.invalid)) {
        violations.push(`field '${d.field}': unknown invalid-policy '${d.invalid}'`);
      }
      if (d.readBy.includes("ingest") && d.invalid === null) {
        violations.push(`field '${d.field}': read by ingest but has no invalid-coercion policy`);
      }
    }
    expect(violations).toEqual([]);
  });

  test("every MCP-written field applies to documents (tools only write document notes)", () => {
    for (const d of contract) {
      if (d.writtenBy.length > 0 && !d.appliesTo.includes("documents")) {
        throw new Error(
          `field '${d.field}' is written by ${d.writtenBy.join("/")} but appliesTo lacks 'documents'`,
        );
      }
    }
  });
});

describe("create_note written field set matches the contract", () => {
  const contract = loadContract();
  const contractCreateSet = new Set(
    contract.filter((d) => d.writtenBy.includes("create_note")).map((d) => d.field),
  );

  test("frontmatter keys of a maximal create_note equal the contract's create_note set", async () => {
    const vault = await makeTempVault();
    const config = await loadVaultConfig(vault);
    // Pass every optional frontmatter-bearing arg so conditionally-written
    // fields (confidence, file_ref) are exercised too.
    const result = (await create_note(
      vault,
      {
        owner: TEST_AGENT,
        title: "Contract Probe",
        body: "body",
        tags: ["#tag"],
        concepts: ["Some Concept"],
        status: "draft",
        confidence: "high",
        file_ref: "/data/ref.pdf",
      },
      config,
    )) as { path?: string; error?: string; message?: string };
    expect(result.error).toBeUndefined();
    const content = await fs.readFile(path.join(vault, result.path as string), "utf-8");
    const written = new Set(Object.keys(parseNote(content).metadata));
    // Drift in EITHER direction is a contract violation: a new field written
    // without updating schema/frontmatter-contract.json, or a contract field
    // create_note no longer writes.
    expect(drift(written, contractCreateSet)).toEqual({
      inContractButNotInCode: [],
      inCodeButNotInContract: [],
    });
  });

  test("contract enum values for confidence are exactly what create_note accepts", async () => {
    const descriptor = loadContract().find((d) => d.field === "confidence");
    expect(descriptor?.type).toMatch(/^enum:/);
    const enumValues = (descriptor as FieldDescriptor).type.slice("enum:".length).split("|");
    const vault = await makeTempVault();
    const config = await loadVaultConfig(vault);
    for (const value of enumValues) {
      const ok = (await create_note(
        vault,
        { owner: TEST_AGENT, title: `Conf ${value}`, body: "body", confidence: value as "low" },
        config,
      )) as { error?: string };
      expect(ok.error).toBeUndefined();
    }
    const rejected = (await create_note(
      vault,
      // @ts-expect-error — runtime guard against off-enum strings
      { owner: TEST_AGENT, title: "Conf bad", body: "body", confidence: "off-enum-value" },
      config,
    )) as { error?: string };
    expect(rejected.error).toBe("VALIDATION_ERROR");
  });
});

describe("update_note patchable key set matches the contract", () => {
  test("PATCHABLE_FRONTMATTER_KEYS equals the contract's update_note set", () => {
    const contract = loadContract();
    const contractPatchSet = new Set(
      contract.filter((d) => d.writtenBy.includes("update_note")).map((d) => d.field),
    );
    expect(drift(PATCHABLE_FRONTMATTER_KEYS, contractPatchSet)).toEqual({
      inContractButNotInCode: [],
      inCodeButNotInContract: [],
    });
  });
});

describe("security-frozen fields", () => {
  // These writtenBy values are SECURITY guarantees, not bookkeeping: a
  // patchable `scope` lets a path-authorized caller spoof graph
  // read-visibility (ingest prefers frontmatter scope over the directory),
  // and patchable `source`/`source_agent` forge provenance. Any edit that
  // adds a writer to these descriptors — including a mechanical "fix" chasing
  // a red set-diff elsewhere — must consciously delete this test.
  test("scope/source/source_agent stay unwritable (scope-spoof / provenance-forgery guard)", () => {
    const byField = new Map(loadContract().map((d) => [d.field, d]));
    expect(byField.get("scope")?.writtenBy).toEqual([]);
    expect(byField.get("source")?.writtenBy).toEqual([]);
    // source_agent is stamped once at creation and never patchable after.
    expect(byField.get("source_agent")?.writtenBy).toEqual(["create_note"]);
  });

  // Behavioral half: proves the running validation path enforces the freeze,
  // so a refactor that keeps the exported PATCHABLE set but stops consulting
  // it still fails here.
  test.each(["scope", "source", "source_agent"])(
    "update_note rejects a frontmatter_patch on '%s' with VALIDATION_ERROR",
    async (field) => {
      const vault = await makeTempVault();
      const config = await loadVaultConfig(vault);
      const created = (await create_note(
        vault,
        { owner: TEST_AGENT, title: `Freeze ${field}`, body: "body" },
        config,
      )) as { id: string };
      const result = (await update_note(
        vault,
        { owner: TEST_AGENT, id: created.id, frontmatter_patch: { [field]: "spoofed" } },
        config,
      )) as { error?: string; message?: string };
      expect(result.error).toBe("VALIDATION_ERROR");
      expect(result.message).toContain(field);
    },
  );
});
