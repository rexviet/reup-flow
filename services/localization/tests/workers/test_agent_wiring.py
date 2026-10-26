"""What a production worker registers for the Localizer stage, and that it refuses
to start without what the stage needs."""

from types import SimpleNamespace

import pytest
from pydantic_ai.durable_exec.temporal import TemporalDurability

from localization.agents import localizer as localizer_module
from localization.agents import temporal as temporal_module
from localization.config import ConfigError, Settings
from localization.storage.object_store import ObjectStore
from localization.workers.run import bind_agent_runtime, queue_specs, runnable

PROVIDERS = SimpleNamespace(stt=object(), ocr=object(), tts=object(), demucs=object())


def _registered(settings: Settings) -> dict[str, set[str]]:
    """Activity names each queue's worker serves, counting the agent's plugin."""
    agent_names = {
        fn.__temporal_activity_definition.name  # type: ignore[attr-defined]
        for fn in TemporalDurability.from_agent(temporal_module.localizer).temporal_activities  # type: ignore[union-attr]
    }
    specs = runnable(queue_specs("agents", settings, ObjectStore(object(), "b"), PROVIDERS))  # type: ignore[arg-type]
    out: dict[str, set[str]] = {}
    for spec in specs:
        names = {fn.__temporal_activity_definition.name for fn in spec.activities}  # type: ignore[attr-defined]
        if spec.plugins:
            names |= agent_names
        out[spec.name] = names
    return out


def test_each_part_of_the_stage_is_served_on_the_queue_the_workflow_sends_it_to() -> None:
    served = _registered(Settings())
    assert "voice_rate" in served["tts"]
    assert "assemble_script" in served["io"]
    assert "agent__localizer__model_request" in served["llm"]
    assert "agent__localizer__toolset__<agent>__call_tool" in served["io"]


def test_the_llm_queue_has_a_worker_even_though_it_has_no_plain_activities() -> None:
    specs = runnable(queue_specs("agents", Settings(), ObjectStore(object(), "b"), PROVIDERS))  # type: ignore[arg-type]
    assert "llm" in {s.name for s in specs}


def test_the_queues_the_agent_is_pinned_to_are_the_documented_ones() -> None:
    assert temporal_module.MODEL_ACTIVITY["task_queue"] == "llm"
    assert temporal_module.TOOL_ACTIVITY["task_queue"] == "io"
    for name in localizer_module.IO_TOOLS:
        assert temporal_module.TOOL_METADATA[name]["temporal"] is temporal_module.TOOL_ACTIVITY
    for name in localizer_module.PURE_TOOLS:
        assert temporal_module.TOOL_METADATA[name]["temporal"] is False


@pytest.fixture(autouse=True)
def _unbind() -> object:
    yield
    localizer_module.bind_ports(None)
    temporal_module.bind_model_factory(None)


def _settings(**values: str) -> Settings:
    return Settings(**values)  # type: ignore[arg-type]


STORE = ObjectStore(object(), "b")


def test_the_agents_worker_refuses_to_start_without_a_database() -> None:
    settings = _settings(
        localizer_model="some/model",
        openrouter_api_key="k",  # type: ignore[arg-type]
    )
    with pytest.raises(ConfigError, match="LOC_DATABASE_URL"):
        bind_agent_runtime(settings, STORE)


def test_the_agents_worker_refuses_to_start_without_a_model() -> None:
    settings = _settings(
        loc_database_url="postgresql://x/y",
        openrouter_api_key="k",  # type: ignore[arg-type]
    )
    with pytest.raises(ConfigError, match="LOCALIZER_MODEL"):
        bind_agent_runtime(settings, STORE)


def test_the_agents_worker_refuses_to_start_without_a_provider_key() -> None:
    settings = _settings(
        loc_database_url="postgresql://x/y",
        localizer_model="some/model",  # type: ignore[arg-type]
    )
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        bind_agent_runtime(settings, STORE)


def test_a_configured_worker_has_its_ports_and_model_bound() -> None:
    settings = _settings(
        loc_database_url="postgresql://x/y",  # type: ignore[arg-type]
        localizer_model="openai:some/model",  # type: ignore[arg-type]
        openrouter_api_key="k",  # type: ignore[arg-type]
    )
    bind_agent_runtime(settings, STORE)
    assert localizer_module._ports is not None  # noqa: SLF001
    assert temporal_module._model_factory is not None  # noqa: SLF001
    assert temporal_module._model_factory().model_name == "some/model"  # noqa: SLF001
