"""A workflow that only runs the slot shortener, as the Vbee dubbing workflow will: the
durable agents, their model activity on the `llm` queue and the checks in workflow code."""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from localization.agents.temporal import (
        run_slot_shortener,
        slot_shortener_agent,
        slot_shortener_repair_agent,
    )
    from localization.contracts.base import Contract
    from localization.contracts.vbee_flow import SlotPlan

TASK_QUEUE = "shortener-workflows"


class ShortenInput(Contract):
    plan: SlotPlan
    syllables_per_sec: float


@workflow.defn(name="SlotShortenerFlow")
class SlotShortenerFlow:
    __pydantic_ai_agents__ = [slot_shortener_agent, slot_shortener_repair_agent]

    @workflow.run
    async def run(self, spec: ShortenInput) -> SlotPlan:
        return await run_slot_shortener(spec.plan, spec.syllables_per_sec)
