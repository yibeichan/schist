export const SNIPPET_MAX_CODE_POINTS = 200;
const ELLIPSIS = "…"; // U+2026 HORIZONTAL ELLIPSIS

/**
 * Trims `content` to at most `maxCodePoints` Unicode code points (NOT UTF-16
 * code units), appending the ellipsis "…" iff truncation occurred. The
 * code-point spread (`[...str]`) is used because `str.slice(0, N)` slices
 * UTF-16 units and can split surrogate pairs mid-character (e.g. an emoji at
 * the boundary becomes half a surrogate).
 *
 * Returns the input unchanged when it already fits.
 */
export function snippetContent(content: string, maxCodePoints: number = SNIPPET_MAX_CODE_POINTS): string {
  const codePoints = [...content];
  if (codePoints.length <= maxCodePoints) return content;
  return codePoints.slice(0, maxCodePoints).join("") + ELLIPSIS;
}
