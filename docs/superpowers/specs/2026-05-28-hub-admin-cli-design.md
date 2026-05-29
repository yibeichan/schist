# Hub ACL admin CLI — design (#154)

**Date:** 2026-05-28
**Issue:** #154 — *Hub ACL: no CLI to grant a participant write-scope on a new
directory after `init --hub` (schema ↔ vault.yaml drift)*
**Branch:** `feat/issue-154-hub-admin-cli`

## Problem

In a bare-repo hub (Option A, `pre-receive` ACL), `schist init --hub` seeds each
participant's `write:` scope in `vault.yaml` **once** (`_build_seed_vault`,
`sync.py:633`). When the shared schema later gains a content directory, the
participants' `write:` lists are not updated. A spoke that writes to the new
directory has its push rejected by pre-receive, and there is **no command** to
update participant ACLs after init.

The only path today is hand-editing `vault.yaml` on the hub and committing it —
and that is itself blocked over the network: `vault.yaml` is a root file, and
pre-receive (`pre_receive.py:118-121`) requires `'*'` in the pushing identity's
`write:` list to modify any root file. The seed grants no participant `'*'`, so
**no participant can push a `vault.yaml` edit at all**. ACL administration has no
supported mechanism.

`doctor.py:351 check_spoke_acl_drift` already covers the issue's requested
spoke-side diagnostic (#3) — a spoke is warned when a local schema directory is
not in its hub write grant. This design covers the remaining items: the hub
admin CLI (#1), a hub-mode drift lint (#2), and docs (#4).

## Decisions

These design forks were resolved during brainstorming:

1. **Admin authority = shell access to the hub host (filesystem), not an ACL
   participant.** The CLI commits `vault.yaml` changes directly into the bare
   repo via git plumbing, never invoking `receive-pack`, so pre-receive never
   fires. This is correct: pre-receive guards the *network* boundary; a
   filesystem operator is already inside the trust boundary, and is the same
   credential already required to *create* the hub via `init --hub`. No new ACL
   concept, no new init flag, zero backward-compat migration (works on every
   existing bare-repo hub immediately).

   Rejected alternatives: an admin participant holding `write: ['*']` (puts
   ACL-rewrite power on a network-reachable, propagated SSH credential, and
   conflates *policy-admin* with *all-content-write* because `'*'` is both); a
   hybrid (adds that escalation surface back).

2. **Full subcommand set:** `grant`, `revoke`, `participant add`,
   `participant rename`, `participant remove`.

3. **`participant rename` rekeys hub-side only, with a loud warning.** It rekeys
   the `participants` entry and the `access` map key on the hub. It cannot reach
   the renamed spoke's local `.schist/spoke.yaml` (whose `identity` must change
   or its pushes are rejected), and it does **not** rewrite the
   `source_agent: <old>` stamped into already-written notes (append-only
   invariant). The command prints an explicit ACTION-REQUIRED warning naming
   both consequences.

4. **`grant` refuses `--write '*'`.** Because `'*'` write is also the
   pre-receive gate for editing `vault.yaml`, granting it to a participant would
   re-open the exact remote ACL-rewrite escalation that Decision 1 closes. The
   CLI rejects it with a message directing the operator to administer ACLs from
   the hub host and grant concrete directories instead. Invariant locked: **no
   participant ever holds policy-edit power.**

5. **Companions in scope:** hub-mode drift lint in `schist doctor` (#2), using
   **both** schema-vs-grant detection and cross-participant consistency
   detection; and a docs section (#4).

## Architecture & module layout

New module **`cli/schist/hub_admin.py`** holds the feature, keeping the already
large `sync.py` (1029 lines) untouched. Two cleanly separated concerns:

- **Pure mutation functions** operating on the parsed `vault.yaml` dict —
  `grant`, `revoke`, `participant_add`, `participant_rename`,
  `participant_remove`. No I/O; fully unit-testable on dict fixtures.
- **One I/O helper** `commit_vault_yaml(hub_path, new_text, message)` that lands
  the change into the bare repo.

CLI dispatch adds a `hub` subparser in `__main__.py`. Hub-mode drift lint adds
`check_hub_acl_drift()` in `doctor.py`. Reuses `acl.py`'s `parse_vault_data`,
`NAME_RE`, `_validate_scope`, `ACLError`.

## The commit mechanism (robust core)

Direct plumbing into the bare repo, atomic via compare-and-swap. Never invokes
`receive-pack`, so pre-receive (and post-receive) never fire:

```
old    = git --git-dir=HUB rev-parse HEAD            # CAS baseline
branch = git --git-dir=HUB symbolic-ref --short HEAD
blob   = git --git-dir=HUB hash-object -w --stdin  < new_vault.yaml
# build new tree from HEAD's tree in a throwaway index:
GIT_INDEX_FILE=tmp git --git-dir=HUB read-tree HEAD
GIT_INDEX_FILE=tmp git --git-dir=HUB update-index --add \
    --cacheinfo 100644,<blob>,vault.yaml
tree   = GIT_INDEX_FILE=tmp git --git-dir=HUB write-tree
commit = git --git-dir=HUB commit-tree <tree> -p <old> -m "<msg>"  # schist author env
git --git-dir=HUB update-ref refs/heads/<branch> <commit> <old>    # CAS
```

- `update-ref … <old>` is the compare-and-swap: if another admin committed
  concurrently between our read and write, it fails cleanly ("hub changed,
  retry") rather than clobbering.
- Author/committer identity uses the same `schist`/`schist@local` env defaults
  as `_build_hub_in_staging`.
- Serialization matches init exactly:
  `yaml.dump(data, default_flow_style=False, sort_keys=False)`.
- Branch is resolved from `HEAD` (init creates `main`); no hardcoded branch name.

## Validation flow (fail-closed)

Every mutating command follows the same pipeline:

1. Read `HEAD:vault.yaml` from the bare repo (`git show HEAD:vault.yaml`).
2. `yaml.safe_load` to a dict — preserves **all** fields, not just the ACL view,
   so round-tripping never drops `vault_version`, `name`, `scope_convention`,
   participant `metadata`, etc.
3. Apply the mutation to the dict.
4. Re-validate the mutated dict with strict `parse_vault_data()`.
5. Commit **only if** validation passes; otherwise abort before any write.

This guarantees the CLI never corrupts the hub's `vault.yaml`, and as a side
benefit keeps the hub `vault.yaml` strictly valid (hardening against the #160
structural-invalidity class on the hub side).

## Subcommands

All take `--hub-path <bare-repo>` (required — filesystem admin).

| Command | Behavior | Guards |
|---|---|---|
| `hub grant <p> --write <dir>` | add `<dir>` to `access[p].write` | **refuse `'*'`**; error if `<p>` not a participant; `_validate_scope(<dir>)`; no-op + notice if already granted |
| `hub revoke <p> --write <dir>` | remove `<dir>` from `access[p].write` | warn if not present (idempotent); empty `write` list allowed (read-only participant) |
| `hub participant add <name> [--write dir…] [--read dir…] [--type spoke]` | append to `participants` + create `access[name]` (default `read: ['*']`) | `NAME_RE`; error if name exists; refuse `'*'` in `--write` |
| `hub participant rename <old> <new>` | rekey `participants` entry + `access` key | error if `<old>` missing / `<new>` exists; **loud ACTION-REQUIRED warning** (spoke must update `.schist/spoke.yaml`; historical `source_agent:<old>` left intact) |
| `hub participant remove <name> [--yes]` | drop `participants` entry + `access[name]` | confirm unless `--yes`; notes remain (append-only) |

`--read` on `grant`/`revoke` is out of scope: `read: ['*']` is the seeded default
and read over-grant is harmless. `grant`/`revoke` operate on `write` only.

## Hub-mode drift lint — `schist doctor --hub-path <bare>`

Runs only when `--hub-path` points at a bare repo. Reads `HEAD:vault.yaml` (SKIP
if unreadable). Emits **two** drift signals:

- **(a) Schema-vs-grant.** Source the expected directory set from
  `HEAD:schist.yaml` if present, else fall back to the packaged
  `cli/schist/default.yaml` directory list **minus infra dirs** (`logs/`,
  `projects/`) — matching the seed's deliberate 6-dir grant intent, so the
  deliberately-ungranted infra dirs are not false-positives. Flag any expected
  dir not granted (via `_scope_matches`) to one-or-more participants. WARN with
  a fix-it line, e.g. `schist hub grant bob --write foo`.
- **(b) Cross-participant consistency.** Using `vault.yaml` alone, flag any
  directory that *some* participants have in `write` but others lack. Catches
  the "granted to a, forgot b" case even when no schema file is reachable.

PASS only when neither signal fires.

## Docs

`docs/hub-spoke-setup.md` gains an **"Administering ACLs"** section:

- the `schist hub grant / revoke / participant add|rename|remove` commands;
- the filesystem-admin model — run on the hub host, point at the bare repo with
  `--hub-path`;
- why `'*'` grants are refused (the escalation rationale from Decision 1/4);
- the rename caveat (coordinate the spoke-side `.schist/spoke.yaml` change).

## Testing

- **Unit (mutation fns on dict fixtures):** grant adds / is idempotent / refuses
  `'*'` / errors on unknown participant; revoke removes / idempotent-warns /
  allows empty write; participant add validates name + refuses `'*'`; rename
  rekeys both entry and access key + errors on missing-old / existing-new;
  remove drops both; mutated-dict-fails-validation aborts without writing.
- **Integration (real bare repo):** build a bare repo (reuse an
  `_build_hub_in_staging`-style helper), run each command, assert
  `HEAD:vault.yaml` round-trips and re-parses; assert CAS aborts on a concurrent
  ref move; assert a subsequent spoke push to a newly-granted dir is **accepted**
  by pre-receive (and was rejected before the grant).
- **doctor:** hub fixture with a schema dir granted to nobody → WARN (signal a)
  with the `grant` fix-it line; fixture with a dir granted to one of two
  participants → WARN (signal b).

## Out of scope / follow-ups

- `--read` grant/revoke flags (read over-grant is harmless; add later if needed).
- Auto-detecting an identity change on the spoke side after `rename` (would
  remove the manual `.schist/spoke.yaml` step) — possible future `sync` feature.
- Full TS-parser structural parity for #160 (this design hardens the hub write
  path only).
