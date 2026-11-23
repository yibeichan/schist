# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""Atomic-write temp reporting and operator-confirmed cleanup."""
import os
import subprocess
import sys
import time

import pytest

from schist.stale_temps import find_stale_temps


def cli(vault, *args):
    return subprocess.run([sys.executable, '-m', 'schist', '--vault', str(vault),
                           'cleanup-temps', *args], text=True, capture_output=True)


def old(path):
    path.write_text('data')
    then = time.time() - 7200
    os.utime(path, (then, then))
    return path


def test_cleanup_requires_confirmation_and_keeps_other_entries(tmp_path):
    stage = tmp_path / '.schist/tmp'
    stage.mkdir(parents=True)
    eligible = [old(stage / 'tmpabcdefgh.tmp'), old(stage / '.Résumé.md.42.0123456789ab.tmp')]
    foreign = old(stage / 'unrelated.tmp')
    fresh = stage / 'tmpaaaaaaaa.tmp'
    fresh.touch()
    symlink = stage / 'tmpbbbbbbbb.tmp'
    symlink.symlink_to(foreign)
    assert sorted(find_stale_temps(tmp_path)) == sorted(eligible)
    assert cli(tmp_path).returncode != 0
    assert all(path.exists() for path in eligible)
    result = cli(tmp_path, '--writers-stopped')
    assert result.returncode == 0, result.stderr
    assert all(not path.exists() for path in eligible)
    assert foreign.exists() and fresh.exists() and symlink.is_symlink()


@pytest.mark.parametrize('component', ['.schist', 'tmp'])
def test_cleanup_refuses_symlinked_staging_components(tmp_path, component):
    vault = tmp_path / 'vault'
    vault.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    if component == '.schist':
        (vault / '.schist').symlink_to(outside)
        (outside / 'tmp').mkdir()
        victim = old(outside / 'tmp/tmpabcdefgh.tmp')
    else:
        (vault / '.schist').mkdir()
        (vault / '.schist/tmp').symlink_to(outside)
        victim = old(outside / 'tmpabcdefgh.tmp')
    assert cli(vault, '--writers-stopped').returncode != 0
    assert victim.exists()
