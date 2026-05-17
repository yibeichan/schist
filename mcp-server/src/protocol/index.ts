// Public surface for the cursor + verbose protocol primitives.
// Consumed by tools.ts and sqlite-reader.ts in PRs 3–7.

export {
  CURSOR_TTL_SECONDS,
  CURSOR_LRU_SIZE,
  canonicalizeQueryHash,
  issueCursor,
  decodeCursor,
  recordIssued,
  checkRefusal,
  resetForTesting as resetCursorForTesting,
} from "./cursor.js";

export type {
  CursorErrorCode,
  CursorError,
  InvalidArgError,
  CanonicalizeResult,
  CanonicalizeOptions,
  IssueCursorInput,
  DecodeCursorResult,
  RecordIssuedInput,
  CheckRefusalInput,
  RefusalResult,
} from "./cursor.js";

export {
  VERBOSE_MIN_CODE_POINTS,
  VERBOSE_RATE_LIMIT_PER_MIN,
  VERBOSE_RATE_LIMIT_WINDOW_MS,
  VERBOSE_FREQ_LRU_SIZE,
  parseVerbose,
  logVerbose,
  noteHighFrequency,
  resetForTesting as resetVerboseForTesting,
} from "./verbose.js";

export type {
  ParseVerboseResult,
  LogVerboseInput,
} from "./verbose.js";

export {
  SNIPPET_MAX_CODE_POINTS,
  snippetContent,
} from "./snippet.js";
