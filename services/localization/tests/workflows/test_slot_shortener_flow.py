"""The slot shortener inside a workflow: each model request is an activity of its own
agent on the `llm` queue, carrying the Localizer's answer-only settings, and a plan
where every line fits makes none. The model is a scripted stand-in."""

import uuid

import pytest
from pydantic_ai.durable_exec.temporal import AgentPlugin
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowFailureError

from localization.agents.answer_only import answer_only_settings
from localization.agents.temporal import (
    LLM_QUEUE,
    MODEL_ALIAS,
    bind_model_factory,
    slot_shortener_agent,
    slot_shortener_repair_agent,
)
from localization.contracts.vbee_flow import SlotPlan
from localization.workers.run import QueueSpec, build_worker
from tests.agents.test_slot_shortener import AFTER, BEFORE, LONG, SHORT, Scripted, plan, say
from tests.support.prep_worker import running
from tests.support.slot_shortener_flow import TASK_QUEUE, ShortenInput, SlotShortenerFlow


async def run(client: Client, model: Scripted, slots: SlotPlan, rate: float = 4.0):
    fm = model.model()
    bind_model_factory(lambda: fm, MODEL_ALIAS)
    plugins = [AgentPlugin(a) for a in (slot_shortener_agent, slot_shortener_repair_agent)]
    try:
        async with running(
            [
                build_worker(client, QueueSpec(TASK_QUEUE, workflows=[SlotShortenerFlow])),
                build_worker(client, QueueSpec(LLM_QUEUE, plugins=plugins)),
            ]
        ):
            handle = await client.start_workflow(
                SlotShortenerFlow.run,
                ShortenInput(plan=slots, syllables_per_sec=rate),
                id=f"shorten-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            result = await handle.result()
            history = await handle.fetch_history()
    finally:
        bind_model_factory(None, MODEL_ALIAS)
    scheduled = [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]
    return result, scheduled


async def test_a_line_repaired_once_takes_one_request_of_each_agent(
    temporal_client: Client,
) -> None:
    model = Scripted(answers=(say("mua iPhone"), say(SHORT)))
    result, scheduled = await run(temporal_client, model, plan(BEFORE, LONG, AFTER))
    assert [s.text for s in result.slots] == [BEFORE, SHORT, AFTER]
    assert result.shortened_ids == (1,)
    assert scheduled == [
        "agent__slot_shortener__model_request",
        "agent__slot_shortener_repair__model_request",
    ]
    assert model.settings == [answer_only_settings(1), answer_only_settings(1)]


async def test_a_plan_where_every_line_fits_schedules_no_activity(
    temporal_client: Client,
) -> None:
    model = Scripted(answers=(say(SHORT),))
    slots = plan(BEFORE, "tạm biệt", AFTER)
    result, scheduled = await run(temporal_client, model, slots)
    assert result == slots
    assert scheduled == []
    assert model.requests == 0


async def test_a_rate_that_is_not_positive_fails_the_run_instead_of_retrying(
    temporal_client: Client,
) -> None:
    with pytest.raises(WorkflowFailureError):
        await run(temporal_client, Scripted(answers=(say(SHORT),)), plan(BEFORE, LONG, AFTER), 0)
