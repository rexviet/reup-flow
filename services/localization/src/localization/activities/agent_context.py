"""What the Localizer and the Dubber need in their prompt when they answer in one
request (`agents/localizer_single.py`, `agents/dubber_single.py`), read in an activity so
the workflow stays free of I/O.

The activity also carries the agent mode: it returns nothing for an agent set to `tools`,
which tells the workflow to run the tool loop. The mode is read where the worker is
configured and recorded in the workflow's history through this result, so a replay never
reads settings.
"""

from temporalio import activity

from localization.agents import dubber as dubber_module
from localization.agents import dubber_single, localizer_single
from localization.agents.dubber import DubberDeps
from localization.agents.dubber_single import DubberContext
from localization.agents.localizer import LocalizerDeps, bound_ports
from localization.agents.localizer_single import LocalizerContext
from localization.config import AgentMode


class AgentContextActivities:
    def __init__(self, localizer_mode: AgentMode, dubber_mode: AgentMode) -> None:
        self._localizer_mode = localizer_mode
        self._dubber_mode = dubber_mode

    @activity.defn(name="localizer_context")
    async def localizer_context(self, deps: LocalizerDeps) -> LocalizerContext | None:
        if self._localizer_mode == "tools":
            return None
        return await localizer_single.load_context(deps, bound_ports())

    @activity.defn(name="dubber_context")
    async def dubber_context(self, deps: DubberDeps) -> DubberContext | None:
        if self._dubber_mode == "tools":
            return None
        return await dubber_single.load_context(deps, dubber_module.bound_ports())
