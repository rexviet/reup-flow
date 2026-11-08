"""Failure scenarios must produce real Temporal errors without calling providers."""

from dataclasses import replace

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.simulation.run import mock_activity


@pytest.mark.parametrize("attempt", [1, 2, 3])
async def test_retry_succeeds_only_on_third_attempt(attempt: int) -> None:
    env = ActivityEnvironment()
    env.info = replace(env.info, attempt=attempt)
    run = mock_activity("synthesize_segments")
    if attempt < 3:
        with pytest.raises(ApplicationError) as error:
            await env.run(run, "retry", 0)
        assert error.value.type == "SimulationRetry"
        assert not error.value.non_retryable
    else:
        assert await env.run(run, "retry", 0) == "mock-complete"


async def test_failure_is_terminal_but_other_scenarios_can_publish() -> None:
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as error:
        await env.run(mock_activity("review_before_render"), "failure", 0)
    assert error.value.non_retryable
    assert error.value.type == "SimulationFailure"
    assert await env.run(mock_activity("review_before_render"), "success", 0) == "mock-complete"
    assert await env.run(mock_activity("publish"), "success", 0) == "mock-complete"
