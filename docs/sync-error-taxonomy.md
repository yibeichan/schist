# Sync failure classification: the wire contract

This aggregates knowledge that otherwise only exists as inline comments spread
across `mcp-server/src/tools.ts` and `cli/schist/sync.py` — the shape a
background push/pull failure takes from the moment it happens to the moment
an agent or operator reads it back. Read this before touching
`classifyPushFailure`, `tailOutput`, `blockWriteIfSyncDirty`, or their Python
mirrors; the ordering and anchoring rules here are load-bearing, not
stylistic.

## The taxonomy

Every push/pull failure gets exactly one class, in `PushFailureClass`
(`mcp-server/src/types.ts`):

| Class | Means | Self-clearing? (#531) | Retriable? (#539) | Remedy |
|---|---|---|---|---|
| `acl-rejected` | Hub refused the content — out of scope, bad identity, pinning | No | **Never** | Move the note under a directory your identity may write |
| `rate-limited` | Hub answered, but the push exceeded a limit | No (deliberately — see below) | Only if the hub printed a `Retry after:` window | Wait for the window (or fix the `.gitignore` if it's `notes_per_sync`) |
| `non-fast-forward` | Spoke diverged; auto-recovery (#500) couldn't rebase it | No | Yes | `sync_retry mode=pull-rebase-push` after cleaning the working tree |
| `transport` | Never got an answer — DNS/connect/mid-transfer failure | Yes, within a grace period | Yes | Wait, or check connectivity |
| `timeout` | The MCP's own subprocess budget expired before an answer | Yes, within a grace period | Yes | Same as transport |
| `stale-git-state` | Local git left mid-operation (`index.lock`, a stuck rebase) | No | Yes *(see Known gap below)* | Generic (`syncDirtyRemedy` has no explicit case — see Known gaps) |
| `spawn-failed` | The MCP couldn't even exec the `schist` binary (PATH/ENOENT) | No | Yes *(see Known gap below)* | Pin `SCHIST_BIN`; `schist doctor` diagnoses this |
| `other` | Classified nothing more specific matched | No | Yes | Generic: `sync_retry` after checking `sync_status` |

Two axes matter and they are NOT the same axis:

- **Self-clearing** (`SELF_CLEARING_FAILURE_CLASSES`, `tools.ts`) — does the
  failure fix itself if you just wait and let the background push retry? Only
  `transport`/`timeout` are in this set. `rate-limited` is deliberately
  excluded even though it *does* clear with time: the hub already answered,
  and letting writes pile up while blocked can turn a short wait into a batch
  too large for `notes_per_sync` to ever accept (#531's PR description).
- **Retriable** (`isRetriableFailure`, `tools.ts:1543`) — would running
  `sync_retry` right now plausibly succeed? `cls !== "acl-rejected" &&
  !(cls === "rate-limited" && no Retry-after window)`. Notably **not** a
  mirror of the self-clearing set — `non-fast-forward`/`stale-git-state`/
  `spawn-failed`/`other` are all "retriable" by this formula even though
  retrying alone won't fix a missing binary or a dirty tree. This formula
  predates the write-gate and was never revisited against it; see Known gaps.

## The sentinel grammar

`.schist/last-sync-error` (constant `SYNC_ERROR_SENTINEL`) holds:

```
<ISO-8601 timestamp> push failed [<class>]: <detail>
```

produced by `formatPushFailure(outcome, prefix)` (`tools.ts:1102`), where
`detail = tailOutput(outcomeMessage(outcome))` — the combined
stdout+stderr of the spawned `schist sync push`/`pull` subprocess, truncated.
Read back by `readSyncErrorState` → `parseSyncErrorText` (splits the
timestamp) → `parseFailureClass` (`tools.ts:1132`, a bare regex on the
`[...]` bracket — it does **not** re-run classification, it trusts the
bracket literally) → `sanitizeSentinelContent` (maps non-ASCII to `?`, hard
caps total length at 500 characters on the **read** side, independent of the
write-side cap below).

**The class in the bracket is always correct** — `classifyPushFailure` runs
on the full untruncated text before any truncation happens. What truncation
can break is whether the *evidence* for that class survives into `detail`
for a later reader (a human, or `sync_status`) to see.

### `tailOutput`'s truncation rule (`tools.ts:1082`)

Keeps the **last** 500 characters, on the assumption git's own actionable
line prints last. Two exceptions, both hub/producer output that prints
*before* git's trailing wrapper, so a blind tail is exactly wrong for them:

- The hub's `Retry after: N seconds` line (`RETRY_WINDOW_RE`) — the only
  evidence a rate-limited failure is self-clearing (#539).
- The specific transport producer line `classifyPushFailure` matched
  (`transportEvidenceLine`) — without it a long connect-time failure can
  show `[transport]` with a detail that proves nothing about why (#634).

Both are preserved to **exactly one line each**, never every matching
producer line — unbounded preservation is what would reopen the sentinel to
vault-filename steering (see next section). If you add a third "evidence
that can print before the wrapper" case, follow this shape: one bounded,
anchored line, prepended with a leading `\n` (required — `formatPushFailure`
glues `detail` directly onto `"push failed [<class>]: "` with no separator,
so an unprefixed line lands mid-line and fails its own anchor on the next
read).

## Classification ordering — this is load-bearing (`classifyPushFailure`, `tools.ts:1005`)

In order, first match wins:

1. `outcome.timedOut` → `timeout`
2. spawn error with no `code`/`signal` → `spawn-failed`
3. `HUB_TRANSIENT_RE` (hub's own `ERROR:` line, "hub may be under load") → `transport`
4. `RATE_LIMIT_REJECTION_RE` (anchored on the hub's `REJECTED: rate limit
   exceeded (...)` line) → `rate-limited`
5. `non-fast-forward` / `fetch first` / `updates were rejected` (git's own
   hint block, only emitted for a stale ref) → `non-fast-forward`
6. `isAclRejection` (`HUB_REFUSAL_RE` / `SHELL_REFUSAL_RE` / generic
   `"pre-receive hook declined"`) → `acl-rejected`
7. `isTransportFailure` → `transport`
8. `STALE_STATE_PATTERNS` → `stale-git-state`
9. else → `other`

Rate-limit and hub-transient are tested **before** ACL because both arrive
wrapped in git's generic `(pre-receive hook declined)` line — testing the
generic ACL matcher first would report every rate-limited or hub-overloaded
push as a permanent ACL violation (this exact regression is #501/#535's
history). Non-fast-forward is tested before ACL for the same reason from the
other direction. This order is duplicated (not shared) between
`classifyPushFailure` (TS) and `classify_push_failure` (`cli/schist/sync.py`)
— see Parity below for how the two are kept honest.

## The write gate (#531/#533)

`blockWriteIfSyncDirty` (`tools.ts:570`) runs before every gated write tool.
It reads the CURRENT sentinel (if any) and asks "would writing now make
things worse?", not "did the last push fail?":

- No sentinel, or vault isn't a spoke → allow.
- Class is `transport`/`timeout` (self-clearing) **and** the sentinel is
  younger than `SCHIST_SYNC_OFFLINE_GRACE_MS` (default 24h, `0` disables the
  grace period entirely) → allow, but every gated write tool's response still
  carries `syncWarning` so the agent knows the note hasn't reached the hub
  yet.
- Everything else — including an unparseable sentinel, one with no `[class]`
  marker (pre-#501), or a self-clearing class past its grace period — blocks
  with `SYNC_DIRTY` and a per-class remedy from `syncDirtyRemedy`
  (`tools.ts:544`). This gate fails closed on anything it can't positively
  identify: a classifier bug here becomes real spoke/hub divergence, so it is
  never the optimistic party.

## Parity: why CLI and MCP each have their own copy

`cli/schist/sync.py`'s `_is_network_error`/`_TRANSPORT_PRODUCER_LINE_RE`
(`sync.py:729`, `:786`) mirror the MCP's `TRANSPORT_PATTERNS`/
`TRANSPORT_PRODUCER_LINE_RE` deliberately — the CLI classifies the **raw**
subprocess output before any "Detail:"-style wrapping is applied to it, so a
downstream consumer parsing the CLI's *printed* text (which the MCP does,
via a captured `schist sync push` invocation) needs the wrapping-tolerant
version. The two lists drifted repeatedly before `schema/transport-
classification-parity.json` existed (#594/#601/#604/#605/#606) — it's now the
single behavior corpus both `cli/tests/test_sync.py` and `mcp-server/tests/
transport-classification-parity.test.ts` assert against, including
`steer-*` cases (a vault filename can't forge a match) and `order-*` cases
(a matcher CAN fire on vault-controlled text, and only classification order
keeps the verdict right). Add new phrasings there, not independently to each
side.

**The overarching security rule**, referenced throughout the code as "anchor
on a producer-owned token at line start": every marker above is ordinary
enough English to appear in a vault filename, and the hub echoes offending
filepaths verbatim into its own rejection lines. Matching a bare substring
over combined output made the classifier steerable by anyone with vault
write (#535/#584/#601 are the incident history). Requiring the line to
*start* with a token only `ssh`/`curl`/`fatal`/`remote`/git itself can print
is what makes plain-English phrasings ("broken pipe", "connection reset")
safe to match at all. `remote` is a known, accepted exception to "safe" —
the hub echoes filepaths onto its own `remote:` lines, so ordering (ACL/rate
tested before transport) is the only thing keeping that one from being
forgeable; see `order-hub-acl-echoes-*` in the parity corpus.

## Known gaps (as of 2026-09-19)

Filed as follow-ups on the work this doc describes — don't assume these are
fixed without checking issue state first:

- **#636** — `syncDirtyRemedy` has no `spawn-failed` case, so the write-gate
  block message tells an agent to run `sync_retry` when the actual fix is
  pinning `SCHIST_BIN`. The same gap exists for `stale-git-state` (also no
  explicit case) but is not separately filed as of this writing — note that a
  `stale-git-state`-classed sentinel only exists at all if `triggerSpokePush`'s
  own automatic `sync push --force` retry (gated on `hasStaleGitOperation`,
  `tools.ts:1251`) already ran and failed, so "just force-push" is not
  actually available as a remedy by the time a human/agent sees this class.
- **#637** — `sync_status.last_sync_error.retriable` has no test coverage for
  `acl-rejected` or the other non-rate-limited classes.
- **#638** — the write gate's `timeout`-is-self-clearing path has no
  regression test.
- `isRetriableFailure`'s formula (every class but `acl-rejected`/windowless-
  `rate-limited` is "retriable") was never revisited against the self-
  clearing set the write-gate introduced later — `spawn-failed` and
  `stale-git-state` read as retriable even though retrying alone fixes
  neither.

## File map

| Concern | TypeScript (`mcp-server/src/`) | Python (`cli/schist/`) |
|---|---|---|
| Transport markers | `TRANSPORT_PATTERNS`, `TRANSPORT_PRODUCER_LINE_RE` (`tools.ts:852`) | `_NETWORK_ERROR_MARKERS`, `_TRANSPORT_PRODUCER_LINE_RE` (`sync.py:729`) |
| Classification | `classifyPushFailure` (`tools.ts:1005`) | `classify_push_failure` (`sync.py:938`) |
| Sentinel render/parse | `formatPushFailure` / `parseFailureClass` (`tools.ts:1102`, `:1132`) | inline in `sync_push`/`sync_pull` |
| Retriability | `isRetriableFailure` (`tools.ts:1543`) | n/a (CLI doesn't gate writes) |
| Write gate | `blockWriteIfSyncDirty` (`tools.ts:570`) | n/a |
| Shared behavior corpus | `schema/transport-classification-parity.json`, consumed by `mcp-server/tests/transport-classification-parity.test.ts` and `cli/tests/test_sync.py` | |
