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


async def test_the_setup_names_the_dubbers_mode_and_reads_lines_only_to_answer_in_one_request(
    _ports,
) -> None:
    _, dubbing = _ports
    for mode in ("tools", "code"):
        setup = await AgentContextActivities("single", mode).dubber_setup(dubber_deps((0, 1)))
        assert (setup.mode, setup.context) == (mode, None)
    assert not dubbing.calls
    setup = await AgentContextActivities("tools", "single").dubber_setup(dubber_deps((0, 1)))
    assert setup.mode == "single"
    assert setup.context is not None and [x.id for x in setup.context.lines] == [0, 1]


async def test_a_run_recorded_before_the_code_mode_gets_the_tool_loop_from_a_code_worker(
    _ports,
) -> None:
    """Such a run asks `dubber_context`, which knows two modes: "no lines" is the loop."""
    _, dubbing = _ports
    assert await AgentContextActivities("single", "code").dubber_context(dubber_deps()) is None
    assert not dubbing.calls
