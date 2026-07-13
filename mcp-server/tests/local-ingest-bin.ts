import * as path from "path";

/**
 * Shell script for a test-local `schist-ingest` wrapper (SCHIST_INGEST_BIN).
 *
 * Single-sourced: three suites used to carry byte-identical copies, which is
 * how the #378 fix would have rotted in one of them.
 *
 * The fast path requires the venv to be COMPLETE, not merely present: uv
 * creates .venv/bin/python milliseconds before it installs dependencies, so
 * an interpreter-existence check alone sent parallel jest workers into the
 * half-built venv (ModuleNotFoundError: frontmatter — #378). The import
 * probe closes that window; losers fall through to `uv run`, whose
 * environment global-setup.cjs pre-warms before workers fork.
 */
export function localIngestWrapperScript(repoRoot: string): string {
  const python = path.join(repoRoot, "cli", ".venv", "bin", "python");
  const cliDir = path.join(repoRoot, "cli");
  return [
    "#!/bin/sh",
    `if [ -x ${JSON.stringify(python)} ] && ${JSON.stringify(python)} -c 'import frontmatter' 2>/dev/null; then`,
    `  PYTHONPATH=${JSON.stringify(cliDir)} exec ${JSON.stringify(python)} -m schist.ingest "$@"`,
    "fi",
    `cd ${JSON.stringify(cliDir)} && exec uv run --with . python -m schist.ingest "$@"`,
    "",
  ].join("\n");
}
