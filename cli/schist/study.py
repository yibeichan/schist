# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""Study cards: what is due, the next review, and grading one card.

A study card is a note under `notes/` tagged `learning`, with its review state
in the body (`get_note` drops custom frontmatter, so bold lines are the store):

    **State:** new | learning | known | retired
    **Next review:** YYYY-MM-DD
    **Interval:** N days

and a `## Review log` section with one line per grade.

`schist study due|new|next|status|grade` is the scheduler. The viewer's Study
tab and `schist serve`'s `/api/study/*` routes call the same functions, so the
vault stays the only scheduler: one card, one schedule, wherever a grade is
written.

Grading writes exactly four things — the three state lines and one appended
log line — while holding the vault write lock, and refuses (409) if the card
changed since the reader saw it. `taught` is agent-only: teaching happens in a
session, not on the page.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path, PurePosixPath

from . import git_ops, writable

GRADES: tuple[str, ...] = ("taught", "good", "partial", "missed", "busy", "known")
# `taught` is the first-teach move; it stays agent-only, so the page may not
# claim a card was taught when nobody taught it.
PAGE_GRADES: tuple[str, ...] = ("missed", "partial", "good", "busy", "known")
STATES: tuple[str, ...] = ("new", "learning", "known", "retired")
RETIRE_AFTER = 180   # days; an interval past this is retired
KNOWN_SPOT_CHECK = 90   # days until the one spot check of a card marked known
DEFAULT_LOCK_TIMEOUT = 30.0
# Card standard, checked by `schist study lint`. One idea a reader could
# explain back in a short paragraph, not a textbook section: the detail
# budget is what keeps two cards on the same topic comparable.
CARD_SECTIONS: tuple[str, ...] = (
    "In one line",
    "What it is",
    "Why we chose it",
    "How it works",
    "How it connects to existing work",
    "Where we used it",
    "Check question",
    "Review log",
)
MAX_WORDS = 500
MIN_WORDS = 250
MAX_ONELINE_WORDS = 60


class StudyError(Exception):
    """A card cannot be read, graded, or written as asked."""


class StudyConflict(StudyError):
    """The card changed between the read and the grade."""


class StudyBusy(StudyError):
    """Another writer holds the vault write lock."""


# -- note text ---------------------------------------------------------------

def split_note(text: str) -> tuple[str, str, int]:
    """Return (frontmatter, body, body offset) without changing either.

    Mirrors `study.py`'s front-matter split but keeps the offset, so a grade
    rewrites the body and leaves the frontmatter bytes untouched.
    """
    if not text.startswith("---"):
        return "", text, 0
    end = text.find("\n---", 3)
    if end < 0:
        return "", text, 0
    start = end + 4
    return text[3:end], text[start:], start


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _title(frontmatter: str) -> str:
    """The `title:` value, including YAML's folded and literal block forms.

    Agents do write `title: >-` followed by indented lines; the one-line
    regex would return ">-" and the card would look untitled.
    """
    match = re.search(r"^title:\s*(.*)$", frontmatter, re.M)
    if not match:
        return ""
    raw = match.group(1).strip()
    if raw not in (">", ">-", ">+", "|", "|-", "|+"):
        return _unquote(raw)
    parts: list[str] = []
    for line in frontmatter[match.end():].splitlines():
        if not line.strip():
            if parts:
                break
            continue
        if not line.startswith((" ", "\t")):
            break
        parts.append(line.strip())
    return ("\n" if raw.startswith("|") else " ").join(parts)


def parse_tags(frontmatter: str) -> list[str]:
    tags: list[str] = []
    in_tags = False
    for line in frontmatter.splitlines():
        if re.match(r"^tags:\s*\[", line):
            inner = line.split("[", 1)[1].rsplit("]", 1)[0]
            return [_unquote(t) for t in inner.split(",") if t.strip()]
        if re.match(r"^tags:\s*$", line):
            in_tags = True
            continue
        if in_tags:
            match = re.match(r"^\s+-\s+(.*)$", line)
            if match:
                tags.append(_unquote(match.group(1)))
            elif line.strip():
                in_tags = False
    return tags


def field(body: str, name: str) -> str | None:
    match = re.search(r"^\*\*" + re.escape(name) + r":\*\*\s*(.+?)\s*$", body, re.M)
    return match.group(1) if match else None


def state_of(body: str) -> str:
    return (field(body, "State") or "new").split()[0].lower()


def interval_of(body: str) -> int:
    raw = field(body, "Interval") or ""
    match = re.search(r"\d+", raw)
    return int(match.group()) if match else 0


def next_of(body: str) -> dt.date | None:
    raw = field(body, "Next review")
    if not raw or not raw[:1].isdigit():
        return None
    try:
        return dt.date.fromisoformat(raw.split()[0])
    except ValueError:
        return None


def body_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def card_from_text(card_id: str, text: str) -> dict | None:
    """Parse a card, or None when the note is not a learning card."""
    frontmatter, body, _ = split_note(text)
    tags = parse_tags(frontmatter)
    if "learning" not in tags:
        return None
    title = _title(frontmatter)
    projects = [t[len("project:"):] for t in tags if t.startswith("project:")]
    return {
        "id": card_id,
        "title": title,
        "tags": tags,
        "state": state_of(body),
        "next": next_of(body),
        "interval": interval_of(body),
        "projects": projects,
        "body": body,
        "text": text,
        "sha": body_sha(text),
    }


def public_card(card: dict) -> dict:
    """The JSON shape the viewer and the CLI print (dates as ISO or None)."""
    return {
        "id": card["id"],
        "title": card["title"],
        "projects": card["projects"],
        "state": card["state"],
        "next": card["next"].isoformat() if card["next"] else None,
        "interval": card["interval"],
        "body": card["body"],
        "sha": card["sha"],
    }


# -- reading -----------------------------------------------------------------

def load_cards(vault: str | Path) -> list[dict]:
    """Every learning card under `notes/`, sorted by id."""
    notes = Path(vault) / "notes"
    cards: list[dict] = []
    if not notes.is_dir():
        return cards
    for path in sorted(notes.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        card = card_from_text(f"notes/{path.name}", text)
        if card is not None:
            cards.append(card)
    return cards


def is_due(card: dict, today: dt.date) -> bool:
    # A new card has never been taught, so it is never due; `new` lists it.
    if card["state"] in ("retired", "new"):
        return False
    return card["next"] is None or card["next"] <= today


def next_review(state: str, interval: int, grade: str,
                today: dt.date) -> tuple[str, int, dt.date | None]:
    """Return (state, interval, next date or None) for one grade."""
    tomorrow = today + dt.timedelta(days=1)
    if grade == "busy":
        return state, interval, tomorrow
    if grade == "taught":
        return "learning", 1, tomorrow
    if grade == "known":
        return "known", KNOWN_SPOT_CHECK, today + dt.timedelta(days=KNOWN_SPOT_CHECK)
    if state == "known":
        if grade == "good":
            return "retired", interval, None
        return "learning", 1, tomorrow
    if grade == "missed":
        return "learning", 1, tomorrow
    if grade == "partial":
        new = max(1, int(interval * 1.2 + 0.5))
    else:   # good
        new = max(3, int(interval * 2.5 + 0.5))
    if new > RETIRE_AFTER:
        return "retired", new, None
    return "learning", new, today + dt.timedelta(days=new)


# -- lint --------------------------------------------------------------------

def section_text(body: str, name: str) -> str | None:
    """The text under `## <name>`, or None when the section is absent."""
    lines = body.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == f"## {name}"), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1:end]).strip()


def connection_lines(body: str) -> list[str]:
    """`- <type>: <target>` lines in `## Connections` (the ingest rule)."""
    section = section_text(body, "Connections") or ""
    return [line for line in section.splitlines()
            if re.match(r"^- [A-Za-z][\w-]*:\s+\S", line)]


def _body_before_log(body: str) -> str:
    idx = body.find("\n## Review log")
    return body[:idx] if idx >= 0 else body


def lint_card(card: dict) -> list[str]:
    """Warnings for one card, empty when it meets the card standard."""
    body = card["body"]
    warnings: list[str] = []
    if not card["title"].startswith("Learning:"):
        warnings.append('title does not start with "Learning:"')
    sections = [line[3:].strip() for line in body.splitlines() if line.startswith("## ")]
    for name in CARD_SECTIONS:
        if name not in sections:
            warnings.append(f'missing "## {name}"')
    words = len(_body_before_log(body).split())
    if words > MAX_WORDS:
        warnings.append(f"long: {words} words (target ≤ {MAX_WORDS})")
    elif words < MIN_WORDS:
        warnings.append(f"thin: {words} words (target ≥ {MIN_WORDS})")
    one = section_text(body, "In one line")
    if one is not None and len(one.split()) > MAX_ONELINE_WORDS:
        warnings.append(f'"In one line" is {len(one.split())} words (target ≤ {MAX_ONELINE_WORDS})')
    if not connection_lines(body):
        warnings.append("no links: add ## Connections to the project note or to the note that used it")
    for name in ("State", "Next review", "Interval"):
        hits = len(re.findall(r"^\*\*" + re.escape(name) + r":\*\*", body, re.M))
        if hits != 1:
            warnings.append(f'{hits} "**{name}:**" lines (expected 1)')
    question = section_text(body, "Check question")
    if question is not None and not question:
        warnings.append('"## Check question" is empty')
    return warnings


# -- grading -----------------------------------------------------------------

def normalize_card_id(card_id: str) -> str:
    """`notes/<file>.md` (a bare filename means the same) or StudyError."""
    raw = str(card_id).strip().lstrip("/")
    if not raw.endswith(".md"):
        raise StudyError("card id must end in .md")
    parts = PurePosixPath(raw).parts
    if len(parts) == 1:
        parts = ("notes",) + parts
    if len(parts) != 2 or parts[0] != "notes":
        raise StudyError("card id must be notes/<file>.md")
    if parts[1] in (".", "..") or "\x00" in parts[1]:
        raise StudyError("card id must be notes/<file>.md")
    return f"notes/{parts[1]}"


def _replace_one(lines: list[str], name: str, value: str) -> None:
    pattern = re.compile(r"^\*\*" + re.escape(name) + r":\*\*")
    hits = [i for i, line in enumerate(lines) if pattern.match(line)]
    if len(hits) != 1:
        raise StudyError(
            f'card has {len(hits)} "**{name}:**" lines; expected exactly one'
        )
    lines[hits[0]] = f"**{name}:** {value}"


def _append_log(lines: list[str], line: str) -> None:
    heading = next((i for i, text in enumerate(lines) if text.strip() == "## Review log"), None)
    if heading is None:
        raise StudyError('card has no "## Review log" section')
    end = next((i for i in range(heading + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    last = next((i for i in range(end - 1, heading, -1) if lines[i].strip()), None)
    lines.insert(last + 1 if last is not None else heading + 1, line)


def _log_note(note: str | None) -> str:
    """A review-log answer: one line, at most 200 characters, or empty."""
    if not note:
        return ""
    flat = " ".join(str(note).split())
    if not flat:
        return ""
    return ' · "' + (flat[:199] + "…" if len(flat) > 200 else flat) + '"'


def apply_grade(text: str, grade: str, *, today: dt.date,
                source: str = "session", note: str | None = None) -> tuple[str, dict]:
    """One card's text with only its review fields and log changed."""
    if grade not in GRADES:
        raise StudyError(f"grade must be one of: {', '.join(GRADES)}")
    frontmatter, body, offset = split_note(text)
    old_state, old_interval = state_of(body), interval_of(body)
    new_state, new_interval, new_next = next_review(old_state, old_interval, grade, today)
    lines = body.splitlines()
    _replace_one(lines, "State", new_state)
    _replace_one(lines, "Next review", new_next.isoformat() if new_next else "-")
    _replace_one(lines, "Interval", f"{new_interval} days")
    _append_log(
        lines,
        f"- {today.isoformat()} · {grade} · interval {old_interval}→{new_interval}"
        f" · next {new_next.isoformat() if new_next else '-'} · {source}"
        + _log_note(note),
    )
    new_body = "\n".join(lines) + ("\n" if body.endswith("\n") else "")
    return text[:offset] + new_body, {
        "state": new_state,
        "interval": new_interval,
        "next": new_next,
    }


def grade_card(vault: str | Path, card_id: str, grade: str, *,
               expected_sha: str | None = None, today: dt.date | None = None,
               source: str = "session", note: str | None = None, check=None,
               lock_timeout: float | None = DEFAULT_LOCK_TIMEOUT) -> dict:
    """Grade one card under the vault write lock and commit the change.

    `check`, when given, is called with the vault-relative path first and is
    expected to raise if this spoke may not write it (the viewer passes the
    proposal queue's hub-ACL check). `expected_sha` is the sha256 of the file
    text the caller read; a mismatch is a StudyConflict, checked again under
    the lock so a concurrent grade cannot be overwritten.
    """
    if grade not in GRADES:
        raise StudyError(f"grade must be one of: {', '.join(GRADES)}")
    vault_path = Path(vault)
    rel = normalize_card_id(card_id)
    path = vault_path / rel
    if path.is_symlink():
        raise StudyError(f"{rel} is a symlink; refusing to grade it")
    if not path.is_file():
        raise StudyError(f"no study card at {rel}")
    try:
        path.resolve().relative_to(vault_path.resolve())
    except ValueError:
        raise StudyError(f"{rel} resolves outside the vault") from None
    if check is not None:
        check(rel)
    day = today or dt.date.today()

    try:
        with git_ops.vault_write_lock(str(vault_path), timeout=lock_timeout):
            text = path.read_text(encoding="utf-8")
            card = card_from_text(rel, text)
            if card is None:
                raise StudyError(f"{rel} is not a learning card")
            if expected_sha and body_sha(text) != expected_sha:
                raise StudyConflict(
                    f"{rel} changed since it was read (now {body_sha(text)[:12]})"
                )
            new_text, _ = apply_grade(text, grade, today=day, source=source, note=note)
            path.write_text(new_text, encoding="utf-8")
            ok, output = git_ops.commit(
                str(vault_path), f"study: {card['title'] or rel} — {grade}", [rel]
            )
            if not ok:
                # Leave the card as it was: an ungraded card that did not
                # move is recoverable, an uncommitted grade would block the
                # next pull and surprise the next writer.
                path.write_text(text, encoding="utf-8")
                raise StudyError(
                    f"could not grade {rel}; the commit failed: {output.strip()[:200]}"
                )
    except git_ops.VaultLockTimeout as exc:
        raise StudyBusy(str(exc)) from None
    return card_from_text(rel, new_text)


# -- CLI ---------------------------------------------------------------------

def _print_cards(cards: list[dict], project: str | None, limit: int) -> None:
    far = dt.date.min

    def key(card: dict):
        mine = 0 if (project and project in card["projects"]) else 1
        return (mine, card["next"] or far, card["id"])

    cards = sorted(cards, key=key)
    if limit:
        cards = cards[:limit]
    if not cards:
        print("none")
        return
    for card in cards:
        print("{} {} {} {} {}".format(
            card["next"].isoformat() if card["next"] else "unset",
            card["state"],
            card["projects"][0] if card["projects"] else "-",
            card["id"],
            card["title"],
        ))


def _parse_today(value: str | None) -> dt.date:
    if not value:
        return dt.date.today()
    return dt.date.fromisoformat(value)


def command(args, vault_path: str) -> None:
    """`schist study ...` — the CLI half; exits on error."""
    import sys

    action = args.study_action
    try:
        today = _parse_today(getattr(args, "today", None))
    except ValueError:
        print("Error: --today must be YYYY-MM-DD", file=sys.stderr)
        sys.exit(2)
    try:
        if action in ("due", "new"):
            cards = load_cards(vault_path)
            if action == "due":
                cards = [card for card in cards if is_due(card, today)]
            else:
                cards = [card for card in cards if card["state"] == "new"]
            _print_cards(cards, getattr(args, "project", None), getattr(args, "limit", 0) or 0)
        elif action == "status":
            cards = load_cards(vault_path)
            counts: dict[str, int] = {}
            for card in cards:
                counts[card["state"]] = counts.get(card["state"], 0) + 1
            due = sum(1 for card in cards if is_due(card, today))
            parts = [f"cards={len(cards)}", f"due={due}"]
            parts += [f"{state}={counts[state]}" for state in STATES if counts.get(state)]
            parts += [
                f"{state}={count}" for state, count in sorted(counts.items())
                if state not in STATES
            ]
            print(" ".join(parts))
            per: dict[str, list[int]] = {}
            for card in cards:
                for project in card["projects"] or ["-"]:
                    bucket = per.setdefault(project, [0, 0])
                    bucket[0] += 1
                    bucket[1] += is_due(card, today)
            for project in sorted(per):
                print(f"  {project}: cards={per[project][0]} due={per[project][1]}")
        elif action == "next":
            state, interval, new_next = next_review(args.state, args.interval, args.grade, today)
            print("state={} interval={} next={}".format(
                state, interval, new_next.isoformat() if new_next else "-"
            ))
        elif action == "lint":
            cards = load_cards(vault_path)
            flagged = 0
            for card in cards:
                warnings = lint_card(card)
                words = len(_body_before_log(card["body"]).split())
                links = len(connection_lines(card["body"]))
                print("{}  {}w  {} link{}  {}".format(
                    card["id"], words, links, "" if links == 1 else "s",
                    "ok" if not warnings else "needs work",
                ))
                for warning in warnings:
                    print(f"  - {warning}")
                flagged += 1 if warnings else 0
            print(f"{len(cards)} cards, {flagged} with warnings")
            if getattr(args, "strict", False) and flagged:
                sys.exit(1)
        elif action == "grade":
            def check(relpath: str) -> None:
                # The hub would refuse this commit, so refuse it here first.
                try:
                    writable.check_writable(vault_path, relpath)
                except writable.WritableError as exc:
                    raise StudyError(str(exc)) from None

            card = grade_card(
                vault_path, args.card, args.grade, expected_sha=args.body_sha,
                today=today, source=args.source, note=args.note, check=check,
            )
            print("state={} interval={} next={}".format(
                card["state"], card["interval"],
                card["next"].isoformat() if card["next"] else "-",
            ))
            print(f"card={card['id']} grade={args.grade}")
        else:
            print("Usage: schist study {due|new|next|status|grade}", file=sys.stderr)
            sys.exit(2)
    except StudyBusy as exc:
        print(f"Error: vault is busy: {exc}", file=sys.stderr)
        sys.exit(1)
    except StudyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
