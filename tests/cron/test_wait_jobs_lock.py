"""Strict cron writers refuse to run without confirmed ownership of the jobs lock."""

import pytest


@pytest.mark.parametrize('failure', [None, False, OSError('lock unavailable')])
def test_strict_lock_refuses_degraded_outer_and_nested_entry(monkeypatch, failure):
    from cron import jobs

    def acquire(*_):
        if isinstance(failure, OSError):
            raise failure
        return failure

    monkeypatch.setattr(jobs, '_acquire_flock', acquire)
    with pytest.raises(RuntimeError, match='cross-process jobs lock'), jobs._jobs_lock(strict=True):
        pytest.fail('a strict writer must not run')
    with jobs._jobs_lock():
        with pytest.raises(RuntimeError, match='cross-process jobs lock'), jobs._jobs_lock(strict=True):
            pytest.fail('a nested writer must not inherit a degraded lock')


@pytest.mark.platforms('posix')
def test_strict_lock_can_reenter_only_for_same_profile(tmp_path):
    from cron import jobs

    other = tmp_path / 'other'
    other.mkdir()
    with jobs._jobs_lock(strict=True):
        with jobs._jobs_lock(strict=True):
            pass
        with jobs.use_cron_store(other):
            with pytest.raises(RuntimeError, match='cross-process jobs lock'), jobs._jobs_lock(strict=True):
                pytest.fail('another profile cannot reuse this lock')
