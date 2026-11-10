"""A timer receipt is admission, while ordinary cron keeps its execution outcome."""
import pytest

from cron import executions, jobs, scheduler
from cron.scheduler_bookkeeping import finish_completed_run


@pytest.mark.parametrize("main,success,delivery_error,blocked,expected,outcome", [
    (True, True, None, False, "admitted", "admitted"),
    (True, False, None, False, "error", "suppressed"),
    (True, True, "handoff failed", False, "delivery_failed", "failed"),
    (True, True, None, True, "blocked_config", "suppressed"),
    (False, True, None, False, "ok", "suppressed"),
    (False, False, None, False, "error", "suppressed"),
])
def test_only_successful_main_handoff_is_admitted(
    main, success, delivery_error, blocked, expected, outcome, tmp_path,
):
    with jobs.use_cron_store(tmp_path / "cron"):
        job = jobs.create_job(prompt="fixture", schedule="every 1h", deliver="local",
                              origin={"platform": "hermes-autonomous"} if main else None)
        execution = executions.create_execution(job["id"], source="fixture")
        delivery = scheduler._RunDelivery(job, success, None if success else "admission refused",
                                         delivery_error=delivery_error, blocked_config=blocked)
        assert finish_completed_run(delivery, None, execution["id"])
        assert jobs.get_job(job["id"])["last_status"] == expected
        saved = executions.get_execution(execution["id"])
        assert saved["status"] == ("completed" if success else "failed")
        assert saved["delivery_outcome"] == outcome
