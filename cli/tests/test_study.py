# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for study cards: the scheduler ported from the /study skill's
`test-study.sh`, the field-only grade rewrite, and `schist study grade`'s
lock, hash and path guards."""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

import pytest

from schist import study

TODAY = dt.date(2026, 11, 10)


def _git(vault: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(vault), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def vault(tmp_path) -> Path:
    v = tmp_path / 'vault'
    (v / 'notes').mkdir(parents=True)
    (v / 'schist.yaml').write_text(
        'connection_types: [related]\n'
        'directories:\n  notes: notes/\n', encoding='utf-8')
    (v / '.gitignore').write_text('.schist/\n', encoding='utf-8')
    _git(v, 'init', '-q', '-b', 'main')
    _git(v, 'config', 'user.email', 't@example.com')
    _git(v, 'config', 'user.name', 't')
    return v


def card_text(state: str = 'learning', next_review: str = '2026-11-01',
              interval: int = 3, tags: tuple[str, ...] = ('learning', 'project:demo'),
              title: str = 'Learning: A card') -> str:
    tag_lines = "".join(f"  - '{tag}'\n" for tag in tags)
    return (
        "---\n"
        f"title: '{title}'\n"
        "date: '2026-11-01'\n"
        "tags:\n"
        f"{tag_lines}"
        "status: active\n"
        "---\n"
        f"**State:** {state}\n"
        f"**Next review:** {next_review}\n"
        f"**Interval:** {interval} days\n"
        "\n"
        "## In one line\nA card.\n"
        "\n"
        "## Review log\n"
        "- 2026-11-01 · taught · interval 0→1\n"
    )


def write_card(vault: Path, name: str = 'card.md', **kwargs) -> Path:
    path = vault / 'notes' / name
    path.write_text(card_text(**kwargs), encoding='utf-8')
    return path


def commit(vault: Path, message: str = 'seed') -> None:
    _git(vault, 'add', '-A')
    _git(vault, 'commit', '-qm', message)


def due_ids(vault: Path, project: str | None = None, limit: int = 0) -> list[str]:
    cards = [card for card in study.load_cards(vault) if study.is_due(card, TODAY)]
    far = dt.date.min
    cards.sort(key=lambda card: (
        0 if (project and project in card['projects']) else 1,
        card['next'] or far, card['id']))
    if limit:
        cards = cards[:limit]
    return [card['id'] for card in cards]


# -- reading -----------------------------------------------------------------

def test_due_puts_this_project_first_then_the_oldest(vault):
    write_card(vault, 'a-old.md', next_review='2026-11-01', tags=('learning', 'project:other'))
    write_card(vault, 'b-mine.md', next_review='2026-11-05', tags=('learning', 'project:demo'))
    write_card(vault, 'c-future.md', next_review='2026-12-01', tags=('learning', 'project:demo'))
    write_card(vault, 'd-done.md', state='retired', next_review='2026-01-01')
    write_card(vault, 'e-known.md', state='known', next_review='2026-11-04',
               tags=('learning', 'project:other', 'project:demo'))
    write_card(vault, 'f-nontag.md', tags=('project:demo',))
    write_card(vault, 'g-new.md', state='new', next_review='-')
    write_card(vault, 'h-new2.md', state='new', next_review='-', tags=('learning', 'project:other'))

    assert due_ids(vault, project='demo') == [
        'notes/e-known.md', 'notes/b-mine.md', 'notes/a-old.md']
    assert due_ids(vault) == [
        'notes/a-old.md', 'notes/e-known.md', 'notes/b-mine.md']
    assert due_ids(vault, project='demo', limit=1) == ['notes/e-known.md']
    assert scope_due(vault, dt.date(2026, 10, 1)) == []


def scope_due(vault: Path, day: dt.date) -> list[str]:
    return [card['id'] for card in study.load_cards(vault) if study.is_due(card, day)]


def test_new_lists_untaught_cards_only(vault):
    write_card(vault, 'a-new.md', state='new', next_review='-')
    write_card(vault, 'b-new.md', state='new', next_review='-', tags=('learning', 'project:other'))
    write_card(vault, 'c-learning.md')
    assert [card['id'] for card in study.load_cards(vault) if card['state'] == 'new'] == [
        'notes/a-new.md', 'notes/b-new.md']


def test_non_learning_notes_are_ignored(vault):
    write_card(vault, 'plain.md', tags=('project:demo',))
    assert study.load_cards(vault) == []


def test_card_fields_are_parsed(vault):
    write_card(vault, 'card.md', state='known', next_review='2027-02-04', interval=90)
    card = study.load_cards(vault)[0]
    assert card['state'] == 'known'
    assert card['next'] == dt.date(2027, 2, 4)
    assert card['interval'] == 90
    assert card['projects'] == ['demo']
    assert card['title'] == 'Learning: A card'


# -- schedule ----------------------------------------------------------------

@pytest.mark.parametrize(('state', 'interval', 'grade', 'want'), [
    ('new', 0, 'taught', ('learning', 1, '2026-11-11')),
    ('learning', 1, 'good', ('learning', 3, '2026-11-13')),
    ('learning', 4, 'good', ('learning', 10, '2026-11-20')),
    ('learning', 10, 'partial', ('learning', 12, '2026-11-22')),
    ('learning', 10, 'missed', ('learning', 1, '2026-11-11')),
    ('learning', 10, 'busy', ('learning', 10, '2026-11-11')),
    ('learning', 3, 'known', ('known', 90, '2027-02-08')),
    ('known', 90, 'good', ('retired', 90, None)),
    ('known', 90, 'partial', ('learning', 1, '2026-11-11')),
    ('learning', 80, 'good', ('retired', 200, None)),
])
def test_next_review(state, interval, grade, want):
    got = study.next_review(state, interval, grade, TODAY)
    assert (got[0], got[1], got[2].isoformat() if got[2] else None) == want


def test_unknown_grade_is_refused():
    with pytest.raises(study.StudyError):
        study.apply_grade(card_text(), 'bogus', today=TODAY)


# -- the field rewrite -------------------------------------------------------

def test_apply_grade_touches_only_review_fields_and_log():
    text = card_text()
    new_text, fields = study.apply_grade(text, 'good', today=TODAY, source='page')
    assert fields == {'state': 'learning', 'interval': 8,
                      'next': dt.date(2026, 11, 18)}
    assert "**Next review:** 2026-11-18" in new_text
    assert "**Interval:** 8 days" in new_text
    assert new_text.endswith(
        '- 2026-11-01 · taught · interval 0→1\n'
        '- 2026-11-10 · good · interval 3→8 · next 2026-11-18 · page\n')
    # Everything outside the four changed lines is byte-identical.
    assert new_text.split('**State:**')[0] == text.split('**State:**')[0]
    assert '## In one line\nA card.\n' in new_text
    assert "**First studied:**" not in new_text  # the card had none


def test_apply_grade_keeps_frontmatter_bytes():
    text = card_text().replace("title: 'Learning: A card'", "title: 'Learning: A card'  # keep me")
    new_text, _ = study.apply_grade(text, 'partial', today=TODAY)
    assert new_text.split('---', 2)[0:2] == text.split('---', 2)[0:2]


def test_apply_grade_refuses_a_missing_or_duplicated_field():
    text = card_text().replace('**Interval:** 3 days\n', '')
    with pytest.raises(study.StudyError, match='Interval'):
        study.apply_grade(text, 'good', today=TODAY)
    doubled = card_text().replace(
        '**State:** learning\n', '**State:** learning\n**State:** learning\n')
    with pytest.raises(study.StudyError, match='State'):
        study.apply_grade(doubled, 'good', today=TODAY)


def test_apply_grade_refuses_without_a_review_log():
    text = card_text().split('## Review log')[0]
    with pytest.raises(study.StudyError, match='Review log'):
        study.apply_grade(text, 'good', today=TODAY)


# -- grade_card --------------------------------------------------------------

def test_grade_card_writes_and_commits(vault):
    path = write_card(vault)
    commit(vault)
    card = study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)
    assert card['next'] == dt.date(2026, 11, 18)
    assert card['body'].count('2026-11-10 · good') == 1
    message = _git(vault, 'log', '-1', '--format=%s')
    assert message == 'study: Learning: A card — good'
    assert _git(vault, 'status', '--porcelain') == ''
    # The frontmatter and unrelated sections are untouched.
    assert path.read_text(encoding='utf-8').startswith(
        "---\ntitle: 'Learning: A card'\n")


def test_grade_card_rejects_a_stale_sha(vault):
    write_card(vault)
    commit(vault)
    stale = '0' * 64
    with pytest.raises(study.StudyConflict):
        study.grade_card(vault, 'notes/card.md', 'good', expected_sha=stale, today=TODAY)
    assert '2026-11-10 · good' not in (vault / 'notes' / 'card.md').read_text(encoding='utf-8')


def test_grade_card_accepts_the_current_sha(vault):
    path = write_card(vault)
    commit(vault)
    sha = study.body_sha(path.read_text(encoding='utf-8'))
    study.grade_card(vault, 'notes/card.md', 'partial', expected_sha=sha, today=TODAY)
    assert '2026-11-10 · partial' in path.read_text(encoding='utf-8')


def test_grade_card_refuses_a_non_card(vault):
    (vault / 'notes' / 'plain.md').write_text(card_text(tags=('project:demo',)), encoding='utf-8')
    with pytest.raises(study.StudyError, match='not a learning card'):
        study.grade_card(vault, 'notes/plain.md', 'good', today=TODAY)


def test_grade_card_refuses_an_unknown_grade(vault):
    write_card(vault)
    with pytest.raises(study.StudyError, match='grade must be one of'):
        study.grade_card(vault, 'notes/card.md', 'excellent', today=TODAY)


def test_grade_card_refuses_a_missing_card(vault):
    with pytest.raises(study.StudyError, match='no study card'):
        study.grade_card(vault, 'notes/nope.md', 'good', today=TODAY)


@pytest.mark.parametrize('card_id', [
    'notes/../vault.yaml', 'papers/card.md', 'notes/sub/card.md', 'card.txt',
    '/etc/passwd.md', 'notes/..',
])
def test_grade_card_refuses_paths_outside_notes(vault, card_id):
    with pytest.raises(study.StudyError):
        study.grade_card(vault, card_id, 'good', today=TODAY)


def test_grade_card_refuses_a_symlink(vault, tmp_path):
    write_card(vault)
    target = tmp_path / 'outside.md'
    target.write_text(card_text(), encoding='utf-8')
    (vault / 'notes' / 'link.md').symlink_to(target)
    with pytest.raises(study.StudyError, match='symlink'):
        study.grade_card(vault, 'notes/link.md', 'good', today=TODAY)


def test_grade_card_runs_the_writable_check_before_writing(vault):
    write_card(vault)
    seen = []

    def check(relpath):
        seen.append(relpath)
        raise study.StudyError('spoke may not write notes')

    with pytest.raises(study.StudyError, match='spoke may not write'):
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY, check=check)
    assert seen == ['notes/card.md']


def test_grade_card_rolls_the_card_back_when_the_commit_fails(vault, monkeypatch):
    path = write_card(vault)
    commit(vault)
    before = path.read_text(encoding='utf-8')
    monkeypatch.setattr(study.git_ops, 'commit', lambda *args, **kwargs: (False, 'boom'))
    with pytest.raises(study.StudyError, match='could not grade'):
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)
    assert path.read_text(encoding='utf-8') == before


def test_apply_grade_records_the_answer_in_the_log():
    new_text, _ = study.apply_grade(
        card_text(), 'partial', today=TODAY, note='  right idea,\n\nmissing the null  ')
    assert '- 2026-11-10 · partial · interval 3→4 · next 2026-11-14 · session · "right idea, missing the null"' in new_text


def test_apply_grade_truncates_a_long_answer():
    new_text, _ = study.apply_grade(card_text(), 'good', today=TODAY, note='x' * 500)
    assert '"' + 'x' * 199 + '…"' in new_text


def test_cli_grade_refuses_a_path_the_spoke_cannot_write(vault, capsys):
    import json
    from types import SimpleNamespace
    write_card(vault)
    commit(vault)
    (vault / 'vault.yaml').write_text(
        'vault_version: 1\nname: t\nscope_convention: subdirectory\n'
        'participants:\n  - name: alice\n    type: spoke\n    default_scope: global\n'
        'access:\n  alice:\n    read: ["*"]\n    write: ' + json.dumps(['papers']) + '\n',
        encoding='utf-8')
    (vault / '.schist').mkdir(exist_ok=True)
    (vault / '.schist' / 'spoke.yaml').write_text(
        'hub: file:///nonexistent\nidentity: alice\nscope: global\n', encoding='utf-8')
    args = SimpleNamespace(study_action='grade', card='notes/card.md', grade='good',
                           body_sha=None, today=None, source='session', note=None)
    with pytest.raises(SystemExit) as exc:
        study.command(args, str(vault))
    assert exc.value.code == 1
    assert 'may not write' in capsys.readouterr().err
