"""The activities that read what an agent's prompt carries, and say which mode the worker
runs the agent in."""

import pytest

from localization.activities.agent_context import AgentContextActivities
from localization.agents import dubber as dubber_module
from localization.agents.localizer import bind_ports
from tests.agents.test_dubber import deps as dubber_deps
from tests.agents.test_one_request_agents import deps as localizer_deps
from tests.support.dub_fakes import FakeDubberPorts
from tests.support.localizer_fakes import FakePorts


@pytest.fixture(autouse=True)
def _ports():
    localizing, dubbing = FakePorts(), FakeDubberPorts()
    bind_ports(localizing)
    dubber_module.bind_ports(dubbing)
    yield localizing, dubbing
    bind_ports(None)
    dubber_module.bind_ports(None)


async def test_in_single_mode_each_activity_reads_what_the_prompt_needs(_ports) -> None:
    localizing, dubbing = _ports
    acts = AgentContextActivities("single", "single")
    local = await acts.localizer_context(localizer_deps())
    dub = await acts.dubber_context(dubber_deps((0, 1)))
    assert local is not None and [s.id for s in local.segments] == [0, 1]
    assert dub is not None and [x.id for x in dub.lines] == [0, 1]
    assert localizing.calls["segments"] == 1 and dubbing.calls["segment"] == 2


async def test_in_tools_mode_the_activity_says_so_and_reads_nothing(_ports) -> None:
    localizing, dubbing = _ports
    acts = AgentContextActivities("tools", "tools")
    assert await acts.localizer_context(localizer_deps()) is None
    assert await acts.dubber_context(dubber_deps((0, 1))) is None
    assert not localizing.calls and not dubbing.calls


async def test_each_agent_follows_its_own_setting() -> None:
    acts = AgentContextActivities("tools", "single")
    assert await acts.localizer_context(localizer_deps()) is None
    assert await acts.dubber_context(dubber_deps((0, 1))) is not None
