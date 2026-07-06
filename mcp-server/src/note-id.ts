import * as path from "path";

/**
 * Config-independent shape checks for a vault note id (`<dir>/….md`).
 *
 * Single source for the "id-like string" rule (docs/data-model.md, D4),
 * shared by:
 *   - `validateNoteId` in tools.ts (note tools), which layers the
 *     vault.yaml top-level-directory check on top of this shape core; and
 *   - `add_memory`'s `related_doc` validation, which deliberately checks
 *     ONLY shape: memory must stay writable when the vault is unavailable,
 *     so no config lookup, no existence check, no filesystem or vault-DB
 *     access here — pure string validation.
 *
 * Returns a human-readable reason when the shape is invalid, or null when
 * valid. Callers wrap the reason in their own VALIDATION_ERROR naming the
 * parameter that carried the id. Callers are responsible for the
 * non-empty-string type check (their "required"/"optional" framing differs).
 */
export function noteIdShapeError(id: string): string | null {
  if (id.includes("..") || path.isAbsolute(id)) {
    return "must be a relative path without '..'";
  }
  if (!id.endsWith(".md")) {
    return "must be a .md file";
  }
  const segments = id.split("/");
  if (segments.length < 2) {
    return "must live under a top-level directory (e.g. notes/topic.md)";
  }
  if (segments.some((s) => s.startsWith("."))) {
    return "path segments must not start with '.' (rejects .git, .schist, dotfiles)";
  }
  return null;
}
