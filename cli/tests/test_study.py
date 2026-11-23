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


def test_a_folded_yaml_title_is_parsed(vault):
    path = vault / 'notes' / 'folded.md'
    path.write_text(card_text().replace(
        "title: 'Learning: A card'", "title: >-\n  Learning: A folded\n  title"),
        encoding='utf-8')
    card = study.load_cards(vault)[0]
    assert card['title'] == 'Learning: A folded title'
    assert study.lint_card(card) == [] or not any('title does not start' in w for w in study.lint_card(card))


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


@pytest.mark.parametrize('grade', [g for g in study.GRADES if g != 'missed'])
def test_retired_card_accepts_only_missed(vault, grade):
    path = write_card(vault, state='retired', next_review='none', interval=200)
    commit(vault)
    before = path.read_bytes()
    with pytest.raises(study.StudyError, match='Only Missed'):
        study.grade_card(vault, 'notes/card.md', grade, today=TODAY)
    assert path.read_bytes() == before
    study.grade_card(vault, 'notes/card.md', 'missed', today=TODAY)
    card = study.load_cards(vault)[0]
    assert (card['state'], card['interval'], card['next']) == ('learning', 1, TODAY + dt.timedelta(days=1))


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


def test_apply_grade_keeps_crlf_line_endings():
    text = card_text().replace('\n', '\r\n')
    new_text, _ = study.apply_grade(text, 'good', today=TODAY)
    assert '\n' not in new_text.replace('\r\n', '')
    old, new = text.split('\r\n'), new_text.split('\r\n')
    changed = [a for a, b in zip(old[:-1], new) if a != b]
    assert [a.split(':')[0] for a in changed] == ['**Next review', '**Interval']
    assert len(new) == len(old) + 1
    assert new[-2].startswith('- 2026-11-10 · good') and new[-1] == ''


def test_grade_card_keeps_a_crlf_card_crlf_on_disk(vault):
    path = vault / 'notes' / 'card.md'
    path.write_bytes(card_text().replace('\n', '\r\n').encode('utf-8'))
    commit(vault)
    card = study.load_cards(vault)[0]
    study.grade_card(vault, 'notes/card.md', 'good', today=TODAY, expected_sha=card['sha'])
    data = path.read_bytes()
    assert data.count(b'\n') == data.count(b'\r\n')
    assert _git(vault, 'show', '--numstat', '--format=', 'HEAD').split()[:2] == ['3', '2']


def test_a_bare_cr_card_is_still_a_card_and_can_be_graded(vault):
    path = vault / 'notes' / 'card.md'
    path.write_bytes(card_text().replace('\n', '\r').encode('utf-8'))
    commit(vault)
    cards = study.load_cards(vault)
    assert [c['id'] for c in cards] == ['notes/card.md']
    study.grade_card(vault, 'notes/card.md', 'good', today=TODAY, expected_sha=cards[0]['sha'])
    assert b'\r' not in path.read_bytes()
    assert b'2026-11-10 \xc2\xb7 good' in path.read_bytes()


def test_apply_grade_keeps_unicode_separators_in_the_body():
    para = 'One\N{LINE SEPARATOR}two\x0cthree\x85four.'
    text = card_text().replace('A card.', para)
    new_text, _ = study.apply_grade(text, 'good', today=TODAY)
    assert para in new_text
    old, new = text.split('\n'), new_text.split('\n')
    assert len([1 for a, b in zip(old[:-1], new) if a != b]) == 2
    assert len(new) == len(old) + 1


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


def test_grade_card_refuses_a_card_swapped_for_a_symlink_while_waiting_for_the_lock(
        vault, tmp_path, monkeypatch):
    import contextlib
    path = write_card(vault)
    commit(vault)
    target = tmp_path / 'elsewhere.md'
    target.write_text(card_text(), encoding='utf-8')
    real_lock = study.git_ops.vault_write_lock

    @contextlib.contextmanager
    def swap_then_lock(*args, **kwargs):
        with real_lock(*args, **kwargs):
            path.unlink()
            path.symlink_to(target)
            yield

    monkeypatch.setattr(study.git_ops, 'vault_write_lock', swap_then_lock)
    with pytest.raises(study.StudyError, match='symlink'):
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)
    assert target.read_text(encoding='utf-8') == card_text()


def test_grade_card_refuses_notes_swapped_for_a_symlink_while_waiting_for_the_lock(
        vault, tmp_path, monkeypatch):
    import contextlib
    write_card(vault)
    commit(vault)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'card.md').write_text(card_text(), encoding='utf-8')
    real_lock = study.git_ops.vault_write_lock

    @contextlib.contextmanager
    def swap_then_lock(*args, **kwargs):
        with real_lock(*args, **kwargs):
            (vault / 'notes' / 'card.md').unlink()
            (vault / 'notes').rmdir()
            (vault / 'notes').symlink_to(outside, target_is_directory=True)
            yield

    monkeypatch.setattr(study.git_ops, 'vault_write_lock', swap_then_lock)
    with pytest.raises(study.StudyError, match='outside the vault'):
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)
    assert (outside / 'card.md').read_text(encoding='utf-8') == card_text()


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


def test_grade_card_leaves_nothing_staged_when_a_hook_refuses_the_commit(vault):
    path = write_card(vault)
    commit(vault)
    before = path.read_text(encoding='utf-8')
    hook = vault / '.git' / 'hooks' / 'pre-commit'
    hook.write_text('#!/bin/sh\necho refused >&2\nexit 1\n', encoding='utf-8')
    hook.chmod(0o755)
    with pytest.raises(study.StudyError, match='could not grade'):
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)
    assert path.read_text(encoding='utf-8') == before
    assert _git(vault, 'diff', '--cached') == ''
    assert _git(vault, 'status', '--porcelain') == ''


def test_grade_card_refuses_a_page_grade_for_a_new_card(vault):
    path = write_card(vault, state='new', next_review='-', interval=0)
    commit(vault)
    before = path.read_text(encoding='utf-8')
    head = _git(vault, 'rev-parse', 'HEAD')
    with pytest.raises(study.StudyError, match='not been taught'):
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY, source='page')
    assert path.read_text(encoding='utf-8') == before
    assert _git(vault, 'rev-parse', 'HEAD') == head


def test_grade_card_still_teaches_a_new_card_from_a_session(vault):
    write_card(vault, state='new', next_review='-', interval=0)
    commit(vault)
    card = study.grade_card(vault, 'notes/card.md', 'taught', today=TODAY)
    assert card['state'] == 'learning' and card['interval'] == 1


def test_grade_card_restores_and_unstages_when_the_commit_raises(vault, monkeypatch):
    path = write_card(vault)
    commit(vault)
    before = path.read_text(encoding='utf-8')

    def staging_then_crash(vault_path, message, files):
        _git(vault, 'add', '--', *files)
        raise FileNotFoundError('git')

    monkeypatch.setattr(study.git_ops, 'commit', staging_then_crash)
    with pytest.raises(study.StudyWriteError, match='could not grade'):
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)
    assert path.read_text(encoding='utf-8') == before
    assert _git(vault, 'status', '--porcelain') == ''


def test_grade_card_keeps_a_grade_that_landed_before_an_interrupt(vault, monkeypatch):
    path = write_card(vault)
    commit(vault)
    real_commit = study.git_ops.commit

    def commit_then_interrupt(*args, **kwargs):
        real_commit(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(study.git_ops, 'commit', commit_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)
    assert ' · good · ' in path.read_text(encoding='utf-8')
    assert _git(vault, 'status', '--porcelain') == ''
    assert _git(vault, 'log', '-1', '--format=%s').startswith('study:')


def test_grade_card_reports_an_unknown_commit_state_when_git_cannot_start(vault, monkeypatch):
    write_card(vault)
    commit(vault)

    def commit_with_git_gone(vault_path, message, files):
        monkeypatch.setenv('PATH', '')
        raise FileNotFoundError('git')

    monkeypatch.setattr(study.git_ops, 'commit', commit_with_git_gone)
    with pytest.raises(study.StudyWriteError, match='unknown whether the commit landed'):
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)


def test_grade_card_says_so_when_git_cannot_start_before_anything_is_written(vault, monkeypatch):
    path = write_card(vault)
    commit(vault)
    before = path.read_bytes()
    monkeypatch.setattr(study.git_ops, '_head_sha',
                        lambda vault_path: (_ for _ in ()).throw(FileNotFoundError('git')))
    with pytest.raises(study.StudyWriteError, match='git could not be run'):
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)
    assert path.read_bytes() == before


def test_grade_card_says_so_when_the_rollback_fails_too(vault, monkeypatch):
    path = write_card(vault)
    commit(vault)

    def staging_then_crash(vault_path, message, files):
        _git(vault, 'add', '--', *files)
        raise OSError('fork failed')

    monkeypatch.setattr(study.git_ops, 'commit', staging_then_crash)
    monkeypatch.setattr(study, '_unstage', lambda *args: 'git reset failed')
    with pytest.raises(study.StudyWriteError) as exc:
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)
    assert 'may still be on disk or staged' in str(exc.value)
    assert 'git reset failed' in str(exc.value)


def test_a_silent_commit_failure_still_gives_a_reason(vault, monkeypatch):
    write_card(vault)
    commit(vault)
    monkeypatch.setattr(study.git_ops, 'commit', lambda *a, **k: (False, ''))
    with pytest.raises(study.StudyError) as exc:
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)
    assert not str(exc.value).endswith(': ')


def test_grade_card_says_so_when_the_rollback_fails_after_a_failed_commit(vault, monkeypatch):
    write_card(vault)
    commit(vault)
    monkeypatch.setattr(study.git_ops, 'commit', lambda *a, **k: (False, 'boom'))
    monkeypatch.setattr(study, '_unstage', lambda *args: 'git reset failed')
    with pytest.raises(study.StudyError, match='may still be on disk or staged'):
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)


def test_write_errors_do_not_carry_absolute_paths(vault, monkeypatch):
    write_card(vault)
    commit(vault)

    def denied(src, dst):
        raise OSError(13, 'Permission denied', str(vault / 'notes' / 'card.md'))

    monkeypatch.setattr(study.markdown_io.os, 'replace', denied)
    with pytest.raises(study.StudyWriteError) as exc:
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)
    assert 'notes/card.md' in str(exc.value) and 'Permission denied' in str(exc.value)
    assert str(vault) not in str(exc.value)
    monkeypatch.undo()

    def unreadable(self):
        raise OSError(13, 'Permission denied', str(vault / 'notes' / 'card.md'))

    monkeypatch.setattr(Path, 'read_bytes', unreadable)
    with pytest.raises(study.StudyWriteError) as exc:
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)
    assert 'Permission denied' in str(exc.value) and str(vault) not in str(exc.value)


def test_grade_card_leaves_the_card_whole_when_the_write_fails(vault, monkeypatch):
    path = write_card(vault)
    commit(vault)
    before = path.read_bytes()

    def disk_full(src, dst):
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(study.markdown_io.os, 'replace', disk_full)
    with pytest.raises(study.StudyWriteError, match='No space'):
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert _git(vault, 'status', '--porcelain') == ''
    assert not list((vault / '.schist' / 'tmp').glob('*'))


def test_grade_card_refuses_an_intent_to_add_card(vault):
    path = vault / 'notes' / 'card.md'
    path.write_text(card_text(), encoding='utf-8')
    _git(vault, 'add', '-N', '--', 'notes/card.md')
    before = path.read_bytes()
    with pytest.raises(study.StudyError, match='staged changes'):
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)
    assert path.read_bytes() == before
    assert _git(vault, 'status', '--porcelain', '--', 'notes/card.md') == 'A notes/card.md'


@pytest.mark.parametrize('worktree_edit', [False, True], ids=['staged-X-worktree-X', 'staged-X-worktree-Y'])
def test_grade_card_refuses_a_card_with_staged_changes(vault, worktree_edit):
    path = write_card(vault)
    commit(vault)
    path.write_text(card_text() + 'staged edit\n', encoding='utf-8')
    _git(vault, 'add', '--', 'notes/card.md')
    if worktree_edit:
        path.write_text(card_text() + 'staged edit\nand more\n', encoding='utf-8')
    hook = vault / '.git' / 'hooks' / 'pre-commit'
    hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
    hook.chmod(0o755)
    before_file = path.read_bytes()
    before_index = _git(vault, 'ls-files', '-s', '--', 'notes/card.md')
    before_cached = _git(vault, 'diff', '--cached')
    head = _git(vault, 'rev-parse', 'HEAD')
    with pytest.raises(study.StudyError, match='staged changes'):
        study.grade_card(vault, 'notes/card.md', 'good', today=TODAY)
    assert path.read_bytes() == before_file
    assert _git(vault, 'ls-files', '-s', '--', 'notes/card.md') == before_index
    assert _git(vault, 'diff', '--cached') == before_cached
    assert _git(vault, 'rev-parse', 'HEAD') == head


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


def full_card_text() -> str:
    """A card that meets the standard: all sections, links, 300+ words."""
    return (
        "---\n"
        "title: 'Learning: A complete card'\n"
        "date: '2026-11-09'\n"
        "tags:\n  - learning\n  - 'project:demo'\n"
        "status: active\n---\n"
        "**State:** learning\n"
        "**Next review:** 2026-11-10\n"
        "**Interval:** 3 days\n"
        "**First studied:** 2026-11-09 (demo)\n"
        "**Projects:** demo\n"
        "\n"
        "## In one line\n"
        "A single idea, stated once, in the words the project would use for it.\n"
        "\n"
        "## What it is\n"
        "The concept is the mechanism that turns an input into the thing the analysis compares. "
        "It is not the implementation and not the paper; it is the move a reader has to be able to "
        "reproduce in a sentence. When the mechanism has a parameter, the card names it and says "
        "what changing it does. When the mechanism has a known failure mode, the card names that too. "
        "The same idea often appears under two names in the literature, so the card picks the name "
        "this project uses and keeps the other in the connections section. A reader who can explain "
        "the mechanism, the parameter and the failure mode has the card; anything else is detail "
        "that belongs in the note the card links to.\n"
        "\n"
        "## Why we chose it\n"
        "This project needed a comparison that survived the dependence between units. "
        "The alternatives were simpler but would have answered a different question, and the "
        "decision note records why they were not taken. The choice was made once, before the "
        "results were seen, and the card exists so a later session does not relitigate it from "
        "memory. The decision note is the place for the alternatives; the card keeps the reason "
        "short enough to say out loud.\n"
        "\n"
        "## How it works\n"
        "1. Build the two things being compared.\n"
        "2. Compute the statistic on them.\n"
        "3. Replace the labels that carry the confound, recompute, and repeat.\n"
        "4. Compare the observed value with that null.\n"
        "\n"
        "## How it connects to existing work\n"
        "The original test is older and simpler; later work showed the unconstrained version is "
        "anti-conservative when the covariate drifts, which is why the restricted form is used here.\n"
        "\n"
        "## Where we used it\n"
        "- notes/2026-11-09-demo.md (the analysis that used it)\n"
        "\n"
        "## Connections\n"
        "- related: notes/2026-11-09-demo.md \"the analysis this card explains\"\n"
        "\n"
        "## Check question\n"
        "Why would the unrestricted version give a misleadingly small p-value here?\n"
        "\n"
        "## Review log\n"
        "- 2026-11-09 · taught · interval 0→1\n"
    )


def test_lint_passes_a_card_that_meets_the_standard(vault):
    path = vault / 'notes' / 'complete.md'
    path.write_text(full_card_text(), encoding='utf-8')
    card = study.load_cards(vault)[0]
    assert study.lint_card(card) == []
    assert len(study.connection_lines(card['body'])) == 1


def test_lint_flags_the_minimal_fixture_card(vault):
    write_card(vault)
    card = study.load_cards(vault)[0]
    warnings = study.lint_card(card)
    assert any('missing "## What it is"' in w for w in warnings)
    assert any('no links' in w for w in warnings)
    assert any(w.startswith('thin:') for w in warnings)


def test_lint_flags_a_long_card(vault):
    text = full_card_text().replace(
        "\n## Why we chose it\n",
        "\n## Why we chose it\n" + ("padding " * 260) + "\n")
    (vault / 'notes' / 'long.md').write_text(text, encoding='utf-8')
    card = study.load_cards(vault)[0]
    assert any(w.startswith('long:') for w in study.lint_card(card))


def test_lint_flags_a_missing_connections_section(vault):
    text = full_card_text().split("## Connections")[0] + "## Check question\nq\n\n## Review log\n"
    (vault / 'notes' / 'unlinked.md').write_text(text, encoding='utf-8')
    card = study.load_cards(vault)[0]
    assert any('no links' in w for w in study.lint_card(card))


@pytest.mark.parametrize('section', [
    '> a quote first\n- related: notes/x.md "why"\n',
    '---\n- related: notes/x.md "why"\n',
    '### sub\n- related: notes/x.md "why"\n',
    '    - related: notes/x.md "why"\n',
], ids=['blockquote', 'rule', 'subheading', 'indented'])
def test_lint_counts_only_the_connections_ingest_indexes(vault, section):
    from schist import ingest
    text = full_card_text().replace(
        '- related: notes/2026-11-09-demo.md "the analysis this card explains"\n', section)
    assert text != full_card_text()
    (vault / 'notes' / 'odd.md').write_text(text, encoding='utf-8')
    card = study.load_cards(vault)[0]
    assert ingest.parse_connections(card['body']) == []
    assert study.connection_lines(card['body']) == []
    assert any('no links' in w for w in study.lint_card(card))


def test_cli_usage_line_lists_lint(vault, capsys):
    from types import SimpleNamespace
    with pytest.raises(SystemExit) as exc:
        study.command(SimpleNamespace(study_action=None, today=None), str(vault))
    assert exc.value.code == 2
    assert 'lint' in capsys.readouterr().err


def test_lint_cli_strict_exits_1_for_a_warning(vault, capsys):
    from types import SimpleNamespace
    write_card(vault)
    args = SimpleNamespace(study_action='lint', strict=True, today=None)
    with pytest.raises(SystemExit) as exc:
        study.command(args, str(vault))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert 'needs work' in out and '1 cards, 1 with warnings' in out
