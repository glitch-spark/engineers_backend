"""The resume queue and the Job Applies worker only run where the deployment opts in.

A local backend shares the production database, so a worker that ran by default would take real users' jobs
(and run them with local code and local browsers).
"""
import pytest

from app import resume_queue
from app.job_apply import worker

FLAGS = [
    (resume_queue.queue_enabled, "RESUME_QUEUE_ENABLED", "RESUME_QUEUE_DISABLED"),
    (worker.worker_enabled, "JOB_APPLY_WORKER_ENABLED", "JOB_APPLY_WORKER_DISABLED"),
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for _, enabled, disabled in FLAGS:
        monkeypatch.delenv(enabled, raising=False)
        monkeypatch.delenv(disabled, raising=False)


@pytest.mark.parametrize("is_enabled, enabled, disabled", FLAGS)
def test_off_by_default(is_enabled, enabled, disabled):
    assert is_enabled() is False


@pytest.mark.parametrize("is_enabled, enabled, disabled", FLAGS)
def test_on_when_opted_in(monkeypatch, is_enabled, enabled, disabled):
    monkeypatch.setenv(enabled, "1")
    assert is_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
@pytest.mark.parametrize("is_enabled, enabled, disabled", FLAGS)
def test_only_1_opts_in(monkeypatch, is_enabled, enabled, disabled, value):
    monkeypatch.setenv(enabled, value)
    assert is_enabled() is False


@pytest.mark.parametrize("is_enabled, enabled, disabled", FLAGS)
def test_disabled_overrides_enabled(monkeypatch, is_enabled, enabled, disabled):
    monkeypatch.setenv(enabled, "1")
    monkeypatch.setenv(disabled, "1")
    assert is_enabled() is False
