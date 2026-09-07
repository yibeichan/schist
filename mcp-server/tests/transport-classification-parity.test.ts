/**
 * classifyPushFailure's transport branch and the CLI's `_is_network_error`
 * must agree on the same git stderr. They did not: the two vocabularies were
 * hand-maintained and drifted in BOTH directions (#594 #601 #604 #605 #606),
 * and nothing in CI could see it — `schema/` had eight parity fixtures and
 * none covered push-failure classification (#543 names the same gap for the
 * hub vocabulary, which is a separate axis and stays open).
 *
 * schema/transport-classification-parity.json is the single source of truth,
 * consumed here and by cli/tests/test_sync.py.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { classifyPushFailure } from "../src/tools.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type TransportCase = {
  name: string;
  input: string;
  network: boolean;
  mcp_class: string | null;
  why: string;
  mcp_skip_why?: string;
};

function loadCases(): TransportCase[] {
  const fixturePath = path.resolve(
    __dirname, "..", "..", "schema", "transport-classification-parity.json");
  return (JSON.parse(readFileSync(fixturePath, "utf-8")) as { cases: TransportCase[] }).cases;
}

const failed = (stderr: string) => ({ ok: false, code: 1, stdout: "", stderr });

describe("transport-classification parity (#604)", () => {
  const cases = loadCases();

  test("fixture is present and non-trivial", () => {
    // An emptied or mangled fixture must fail loudly rather than leaving a
    // zero-case loop that reports green.
    expect(cases.length).toBeGreaterThanOrEqual(30);
    expect(cases.filter((c) => c.network).length).toBeGreaterThanOrEqual(15);
    expect(cases.filter((c) => !c.network).length).toBeGreaterThanOrEqual(10);
  });

  for (const c of cases.filter((x) => x.mcp_class !== null)) {
    test(`${c.name} -> ${c.mcp_class}`, () => {
      expect(classifyPushFailure(failed(c.input))).toBe(c.mcp_class);
    });
  }

  test("every transport phrasing the fixture asserts is actually reachable", () => {
    // The coverage half. A vocabulary entry with no case behind it is a
    // phrasing nobody has ever exercised — how "recv failure", "send failure"
    // and "empty reply from server" sat untested on the CLI side (#600) and
    // absent here (#604). Asserting a SUBSET, not an intersection: an
    // intersection filter cannot detect absence.
    const transportInputs = cases
      .filter((c) => c.mcp_class === "transport")
      .map((c) => c.input.toLowerCase());
    const unexercised = [
      "could not resolve", "temporary failure in name resolution",
      "failed to connect", "couldn't connect", "connection refused",
      "connection reset", "connection closed", "connection timed out",
      "operation timed out", "recv failure", "send failure",
      "empty reply from server", "network is unreachable", "no route to host",
      "broken pipe", "the remote end hung up", "early eof",
      "kex_exchange_identification",
    ].filter((marker) => !transportInputs.some((i) => i.includes(marker)));
    expect(unexercised).toEqual([]);
  });

  test("an HTTP auth refusal is not transport, and so not retriable (#594)", () => {
    // The specific regression #594 filed: `unable to access` is git's generic
    // HTTP wrapper, so keying on it made a 403 a retriable transport blip.
    const cls = classifyPushFailure(failed(
      "fatal: unable to access 'https://pi.local/vault.git/': " +
      "The requested URL returned error: 403"));
    expect(cls).not.toBe("transport");
  });

  test("a vault filename cannot manufacture a transport verdict", () => {
    // The steering half, stated directly rather than only via the fixture.
    const cls = classifyPushFailure(failed(
      "error: Your local changes to the following files would be overwritten by rebase:\n" +
      "\tnotes/broken pipe.md\n\tnotes/early eof.md\n" +
      "Please commit your changes or stash them before you rebase."));
    expect(cls).not.toBe("transport");
  });
});
