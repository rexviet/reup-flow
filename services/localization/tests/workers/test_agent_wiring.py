"""What a production worker registers for the Localizer stage, and that it refuses
to start without what the stage needs."""

from types import SimpleNamespace

import pytest
from pydantic_ai.durable_exec.temporal import TemporalDurability

from localization.agents import dubber as dubber_module
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
        for agent in (temporal_module.localizer, temporal_module.dubber)
        for fn in TemporalDurability.from_agent(agent).temporal_activities  # type: ignore[union-attr]
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


def test_the_dubbing_stage_is_served_on_the_queues_the_workflow_sends_it_to() -> None:
    served = _registered(Settings())
    assert {"voice_profile", "plan_synthesis", "measure_fit"} <= served["io"]
    assert {"write_dubbed_script", "commit_dub"} <= served["io"]
    assert "synthesize_segments" in served["tts"]
    assert "agent__dubber__model_request" in served["llm"]
    # The Dubber's `synthesize` tool is on `tts`, so the queue's limit covers it.
    assert "agent__dubber__toolset__<agent>__call_tool" in served["tts"]
    media = {
        s.name: {fn.__temporal_activity_definition.name for fn in s.activities}  # type: ignore[attr-defined]
        for s in runnable(queue_specs("media", Settings(), STORE, PROVIDERS))  # type: ignore[arg-type]
    }
    assert "assemble_audio" in media["media"]


def test_the_media_worker_normalises_to_the_configured_loudness() -> None:
    settings = Settings(loudness_lufs=-16.0, true_peak_dbtp=-2.0)
    specs = {s.name: s for s in queue_specs("media", settings, STORE, PROVIDERS)}  # type: ignore[arg-type]
    track = next(
        fn
        for fn in specs["media"].activities
        if fn.__temporal_activity_definition.name == "assemble_audio"  # type: ignore[attr-defined]
    )
    assert (track.__self__._target, track.__self__._peak) == (-16.0, -2.0)  # type: ignore[attr-defined]  # noqa: SLF001


def test_the_media_worker_lays_captions_out_with_the_configured_font_and_cover() -> None:
    settings = Settings(caption_font_path="/fonts/x.ttf", cover_color="#ABCDEF", cover_opacity=0.5)
    specs = {s.name: s for s in queue_specs("media", settings, STORE, PROVIDERS)}  # type: ignore[arg-type]
    layout = next(
        fn
        for fn in specs["media"].activities
        if fn.__temporal_activity_definition.name == "layout_captions"  # type: ignore[attr-defined]
    )
    held = layout.__self__  # type: ignore[attr-defined]
    assert (held._font_path, held._cover_color, held._cover_opacity) == (  # noqa: SLF001
        "/fonts/x.ttf",
        "#abcdef",
        0.5,
    )


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


def test_the_dubbers_tools_are_pinned_to_the_queues_that_limit_them() -> None:
    assert temporal_module.TTS_TOOL_ACTIVITY["task_queue"] == "tts"
    for name in dubber_module.TTS_TOOLS:
        assert temporal_module.DUBBER_TOOL_METADATA[name]["temporal"] is (
            temporal_module.TTS_TOOL_ACTIVITY
        )
    for name in dubber_module.IO_TOOLS:
        assert temporal_module.DUBBER_TOOL_METADATA[name]["temporal"] is (
            temporal_module.TOOL_ACTIVITY
        )
    for name in dubber_module.PURE_TOOLS:
        assert temporal_module.DUBBER_TOOL_METADATA[name]["temporal"] is False
    # Speaking is the only thing the Dubber does that calls the provider.
    assert set(dubber_module.TTS_TOOLS) == {"synthesize"}


@pytest.fixture(autouse=True)
def _unbind() -> object:
    yield
    localizer_module.bind_ports(None)
    dubber_module.bind_ports(None)
    temporal_module.bind_model_factory(None)
    temporal_module.bind_model_factory(None, temporal_module.DUBBER_MODEL_ALIAS)


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


def test_a_worker_without_a_dubber_model_uses_the_localizers() -> None:
    """An env file from before the Dubber existed must still start the worker."""
    settings = _settings(
        loc_database_url="postgresql://x/y",  # type: ignore[arg-type]
        localizer_model="openai:some/model",  # type: ignore[arg-type]
        openrouter_api_key="k",  # type: ignore[arg-type]
    )
    bind_agent_runtime(settings, STORE)
    factories = temporal_module._model_factories  # noqa: SLF001
    assert factories[temporal_module.DUBBER_MODEL_ALIAS]().model_name == "some/model"


def test_an_empty_dubber_model_is_the_same_as_none() -> None:
    settings = _settings(
        loc_database_url="postgresql://x/y",  # type: ignore[arg-type]
        localizer_model="some/model",  # type: ignore[arg-type]
        dubber_model="",  # type: ignore[arg-type]
        openrouter_api_key="k",  # type: ignore[arg-type]
    )
    bind_agent_runtime(settings, STORE)
    factories = temporal_module._model_factories  # noqa: SLF001
    assert factories[temporal_module.DUBBER_MODEL_ALIAS]().model_name == "some/model"


def test_a_configured_worker_has_its_ports_and_models_bound() -> None:
    settings = _settings(
        loc_database_url="postgresql://x/y",  # type: ignore[arg-type]
        localizer_model="openai:some/model",  # type: ignore[arg-type]
        dubber_model="openai:other/model",  # type: ignore[arg-type]
        openrouter_api_key="k",  # type: ignore[arg-type]
    )
    bind_agent_runtime(settings, STORE, tts=object())  # type: ignore[arg-type]
    assert localizer_module._ports is not None  # noqa: SLF001
    assert dubber_module._ports is not None  # noqa: SLF001
    factories = temporal_module._model_factories  # noqa: SLF001
    assert factories[temporal_module.MODEL_ALIAS]().model_name == "some/model"
    assert factories[temporal_module.DUBBER_MODEL_ALIAS]().model_name == "other/model"
