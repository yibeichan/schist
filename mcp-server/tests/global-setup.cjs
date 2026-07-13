const { execFileSync } = require("child_process");
const path = require("path");

/**
 * Pre-warm uv's environment for cli/ BEFORE jest forks workers.
 *
 * The schist-ingest-local wrapper's fallback is `uv run --with .`, and
 * concurrent FIRST-TIME creations of that environment can race each other
 * (observed as uv's "not a compatible environment … cannot be recreated",
 * #378). One warm pass here makes every later invocation a cache hit.
 *
 * Best-effort by design: no uv on PATH or a failed warm just means the
 * first wrapper invocation does the work instead — never fail the suite.
 */
module.exports = async function warmIngestEnv() {
  const cliDir = path.resolve(__dirname, "..", "..", "cli");
  try {
    execFileSync(
      "uv",
      ["run", "--with", ".", "python", "-c", "import schist.ingest"],
      { cwd: cliDir, stdio: "ignore", timeout: 180000 },
    );
  } catch {
    /* best-effort */
  }
};
