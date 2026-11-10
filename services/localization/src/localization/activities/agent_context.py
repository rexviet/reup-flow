"""What the Localizer and the Dubber need in their prompt when they answer in one
request (`agents/localizer_single.py`, `agents/dubber_single.py`), read in an activity so
the workflow stays free of I/O.

The activities also carry the agent mode: `localizer_context` and `dubber_context` return
nothing for an agent set to `tools`, which tells the workflow to run the tool loop, and
`dubber_setup` names the Dubber's mode outright (it has a third, `code`). The mode is read
where the worker is configured and recorded in the workflow's history through this result,
so a replay never reads settings.
"""

from temporalio import activity

from localization.agents import dubber as dubber_module
from localization.agents import dubber_single, localizer_single
from localization.agents.dubber import DubberDeps
from localization.agents.dubber_single import DubberContext
from localization.agents.localizer import LocalizerDeps, bound_ports
from localization.agents.localizer_single import LocalizerContext
from localization.agents.temporal import DubberSetup
from localization.config import AgentMode, DubberMode


class AgentContextActivities:
    def __init__(self, localizer_mode: AgentMode, dubber_mode: DubberMode) -> None:
        self._localizer_mode = localizer_mode
        self._dubber_mode = dubber_mode

    @activity.defn(name="localizer_context")
    async def localizer_context(self, deps: LocalizerDeps) -> LocalizerContext | None:
        if self._localizer_mode == "tools" and not deps.contextual:
            return None
        return await localizer_single.load_context(deps, bound_ports())

    @activity.defn(name="dubber_context")
    async def dubber_context(self, deps: DubberDeps) -> DubberContext | None:
        """For a run recorded before the code mode, which knows only two modes: a worker
        set to `code` answers it as the tool loop, the nearer of the two."""
        if self._dubber_mode != "single":
            return None
        return await dubber_single.load_context(deps, dubber_module.bound_ports())

    @activity.defn(name="dubber_setup")
    async def dubber_setup(self, deps: DubberDeps) -> DubberSetup:
        if self._dubber_mode != "single":
            return DubberSetup(mode=self._dubber_mode)
        context = await dubber_single.load_context(deps, dubber_module.bound_ports())
        return DubberSetup(mode="single", context=context)
