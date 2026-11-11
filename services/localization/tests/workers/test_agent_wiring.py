"""What a production worker registers for the Localizer stage, and that it refuses
to start without what the stage needs."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.durable_exec.temporal import TemporalDurability

from localization.agents import dubber as dubber_module
from localization.agents import localizer as localizer_module
from localization.agents import temporal as temporal_module
from localization.config import ConfigError, Settings
from localization.storage.object_store import ObjectStore
from localization.workers.run import bind_agent_runtime, queue_specs, runnable

PROVIDERS = SimpleNamespace(
    stt=object(),
    ocr=object(),
    tts=object(),
    demucs=object(),
    vbee_stt=None,
    vbee_tts=None,
    singing=None,
)


def _registered(settings: Settings) -> dict[str, set[str]]:
    """Activity names each queue's worker serves, counting the agent's plugin."""
    agent_names = {
        fn.__temporal_activity_definition.name  # type: ignore[attr-defined]
        for agent in (
            temporal_module.localizer,
            temporal_module.dubber,
            temporal_module.localizer_single_agent,
            temporal_module.localizer_repair_agent,
            temporal_module.dubber_single_agent,
            temporal_module.dubber_repair_agent,
        )
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
    assert "localizer_source_evidence" in served["io"]
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


def test_the_review_and_the_publish_are_served_on_the_queues_the_workflow_sends_them_to() -> None:
    served = _registered(Settings())
    assert {"review_before_render", "review_render"} <= served["llm"]
    assert "publish" in served["io"]
    assert "segment_room" in served["io"]
    assert "publish" not in served["llm"] and "review_render" not in served["io"]
    assert "review_before_render" not in served["io"]


def test_the_render_is_served_by_the_media_worker_on_its_own_queue_and_limit() -> None:
    """`render` keeps its own queue, so its limit (RENDER_CONCURRENCY) is not shared with
    the separations and the layout on `media`; the agents worker does not answer it."""
    settings = Settings(render_concurrency=3, caption_font_path="/fonts/x.ttf")
    media = {s.name: s for s in runnable(queue_specs("media", settings, STORE, PROVIDERS))}  # type: ignore[arg-type]
    render = media["render"]
    assert render.limit == 3
    (served,) = render.activities
    assert served.__temporal_activity_definition.name == "render"  # type: ignore[attr-defined]
    assert served.__self__._font_path == "/fonts/x.ttf"  # type: ignore[attr-defined]  # noqa: SLF001
    assert "render" not in {
        fn.__temporal_activity_definition.name  # type: ignore[attr-defined]
        for s in media.values()
        if s.name != "render"
        for fn in s.activities
    }
    agents = runnable(queue_specs("agents", Settings(), STORE, PROVIDERS))  # type: ignore[arg-type]
    assert "render" not in {s.name for s in agents}
    assert Settings().render_concurrency == 1


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


def test_the_agents_worker_refuses_to_start_without_a_reviewer_model() -> None:
    """The vision Reviewer looks at frames, so it has a model of its own: the Localizer's
    may not read images, and a run that reached the review without one would be paid for
    twice."""
    settings = _settings(
        loc_database_url="postgresql://x/y",  # type: ignore[arg-type]
        localizer_model="some/model",  # type: ignore[arg-type]
        reviewer_mode="llm",
        openrouter_api_key="k",  # type: ignore[arg-type]
    )
    with pytest.raises(ConfigError, match="REVIEWER_MODEL"):
        bind_agent_runtime(settings, STORE)


def test_the_jev_reviewer_needs_no_reviewer_model_but_does_need_the_openrouter_key() -> None:
    """The Jev review reads text only, so a worker without REVIEWER_MODEL starts; the key
    it calls with is OpenRouter's, and it is checked at startup like the models' is."""
    configured = _settings(
        loc_database_url="postgresql://x/y",  # type: ignore[arg-type]
        localizer_model="some/model",  # type: ignore[arg-type]
        reviewer_mode="jev",
        openrouter_api_key="k",  # type: ignore[arg-type]
    )
    bind_agent_runtime(configured, STORE)
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        bind_agent_runtime(configured.model_copy(update={"openrouter_api_key": None}), STORE)


def test_the_jev_worker_refuses_to_start_without_the_caption_font(tmp_path: Path) -> None:
    """A worker that starts without it renders a whole video and then fails its review."""
    base = {
        "loc_database_url": "postgresql://x/y",
        "localizer_model": "some/model",
        "reviewer_mode": "jev",
        "openrouter_api_key": "k",
    }
    for font in (tmp_path / "absent.ttf", tmp_path):
        with pytest.raises(ConfigError, match="CAPTION_FONT_PATH"):
            bind_agent_runtime(_settings(**base, caption_font_path=str(font)), STORE)
    broken = tmp_path / "broken.ttf"
    broken.write_bytes(b"not a font")
    with pytest.raises(ConfigError, match="CAPTION_FONT_PATH"):
        bind_agent_runtime(_settings(**base, caption_font_path=str(broken)), STORE)
    bind_agent_runtime(_settings(**base), STORE)  # the real font of the repo
    # The vision reviewer measures no captions, so it does not need the font.
    llm = {**base, "reviewer_mode": "llm", "reviewer_model": "vision/model"}
    bind_agent_runtime(_settings(**llm, caption_font_path=str(tmp_path / "absent.ttf")), STORE)


def test_the_mode_decides_which_reviewer_the_worker_builds() -> None:
    from localization.activities.review_render import ReviewRenderActivity

    def built(mode: str) -> ReviewRenderActivity:
        settings = _settings(reviewer_mode=mode, openrouter_api_key="k")  # type: ignore[arg-type]
        queues = runnable(queue_specs("agents", settings, STORE, PROVIDERS))  # type: ignore[arg-type]
        (llm,) = (q for q in queues if q.name == "llm")
        return llm.activities[0].__self__  # type: ignore[attr-defined,no-any-return]

    assert built("jev")._mode == "jev"  # noqa: SLF001
    assert built("llm")._mode == "llm"  # noqa: SLF001


def test_the_ad_check_is_served_on_llm_with_the_settings_threshold_and_switch() -> None:
    from localization.activities.classify_video import ClassifyVideoActivity

    def built(**values: object) -> ClassifyVideoActivity:
        settings = _settings(openrouter_api_key="k", **values)  # type: ignore[arg-type]
        (llm,) = (q for q in queue_specs("agents", settings, STORE, PROVIDERS) if q.name == "llm")  # type: ignore[arg-type]
        (check,) = (
            fn
            for fn in llm.activities
            if fn.__temporal_activity_definition.name == "classify_video"  # type: ignore[attr-defined]
        )
        return check.__self__  # type: ignore[attr-defined,no-any-return]

    default = built()
    assert (default._threshold, default._enabled) == (0.8, True)  # noqa: SLF001
    tuned = built(ad_gate_threshold=0.95, ad_gate_enabled=False)
    assert (tuned._threshold, tuned._enabled) == (0.95, False)  # noqa: SLF001
    assert "classify_video" not in _registered(Settings())["io"]


def test_the_voice_check_has_a_queue_of_its_own_with_the_settings_threshold_and_switch() -> None:
    """Every run starts with it: on `media` it would wait behind the separations, on `io`
    behind the subtitle detections. Its queue is served by the agents worker only."""
    from localization.activities.check_voice import CheckVoiceActivity, SileroSpeechDetector
    from localization.workflows.video_localization import VOICE_CHECK, VOICE_CHECK_QUEUE

    def built(**values: object) -> tuple[int | None, CheckVoiceActivity]:
        settings = _settings(**values)  # type: ignore[arg-type]
        queues = {q.name: q for q in queue_specs("agents", settings, STORE, PROVIDERS)}  # type: ignore[arg-type]
        (check,) = queues[VOICE_CHECK_QUEUE].activities
        assert check.__temporal_activity_definition.name == "check_voice"  # type: ignore[attr-defined]
        return queues[VOICE_CHECK_QUEUE].limit, check.__self__  # type: ignore[attr-defined]

    assert VOICE_CHECK_QUEUE == VOICE_CHECK["task_queue"] == "voice_check"
    limit, default = built()
    assert (limit, default._min_speech_share, default._enabled) == (2, 0.05, True)  # noqa: SLF001
    assert isinstance(default._detector, SileroSpeechDetector)  # noqa: SLF001
    limit, tuned = built(
        voice_check_min_speech_share=0.1, voice_check_enabled=False, voice_check_concurrency=4
    )
    assert (limit, tuned._min_speech_share, tuned._enabled) == (4, 0.1, False)  # noqa: SLF001
    served = _registered(Settings())
    assert all("check_voice" not in names for q, names in served.items() if q != "voice_check")
    media = runnable(queue_specs("media", Settings(), STORE, PROVIDERS))  # type: ignore[arg-type]
    assert "voice_check" not in {s.name for s in media}
    assert not any(
        fn.__temporal_activity_definition.name == "check_voice"  # type: ignore[attr-defined]
        for s in media
        for fn in s.activities
    )


def test_the_ad_check_threshold_is_a_probability() -> None:
    from pydantic import ValidationError

    for wrong in (-0.1, 1.5):
        with pytest.raises(ValidationError):
            _settings(ad_gate_threshold=wrong)  # type: ignore[arg-type]


def test_jev_is_reached_through_the_openrouter_base_url_without_its_version_segment() -> None:
    """The SDK adds `/v1/systemone` itself: `/api/v1` twice is a 404."""
    assert Settings().jev_base_url == "https://openrouter.ai/api"
    other = Settings(openrouter_base_url="https://proxy.example/openrouter/api/v1/")
    assert other.jev_base_url == "https://proxy.example/openrouter/api"


def test_a_worker_without_a_dubber_model_uses_the_localizers() -> None:
    """An env file from before the Dubber existed must still start the worker."""
    settings = _settings(
        loc_database_url="postgresql://x/y",  # type: ignore[arg-type]
        localizer_model="openai:some/model",  # type: ignore[arg-type]
        reviewer_model="vision/model",  # type: ignore[arg-type]
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
        reviewer_model="vision/model",  # type: ignore[arg-type]
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
        reviewer_model="vision/model",  # type: ignore[arg-type]
        openrouter_api_key="k",  # type: ignore[arg-type]
    )
    bind_agent_runtime(settings, STORE, tts=object())  # type: ignore[arg-type]
    assert localizer_module._ports is not None  # noqa: SLF001
    assert dubber_module._ports is not None  # noqa: SLF001
    factories = temporal_module._model_factories  # noqa: SLF001
    assert factories[temporal_module.MODEL_ALIAS]().model_name == "some/model"
    assert factories[temporal_module.DUBBER_MODEL_ALIAS]().model_name == "other/model"


# --- everything the workflow asks for is served by the production wiring -----------------


def _asked_for(history: Path) -> set[tuple[str, str]]:
    """(activity, queue) of every activity the real workflow scheduled in a recording."""
    events = json.loads(history.read_text())["events"]
    return {
        (a["activityType"]["name"], a["taskQueue"]["name"])
        for e in events
        if (a := e.get("activityTaskScheduledEventAttributes"))
    }


def _served_by_production() -> set[tuple[str, str]]:
    """(activity, queue) the production workers register, with the agents' own activities
    (their model requests and tools) counted on the queues their plugin serves."""
    agent_names = {
        fn.__temporal_activity_definition.name  # type: ignore[attr-defined]
        for agent in (temporal_module.localizer, temporal_module.dubber)
        for fn in TemporalDurability.from_agent(agent).temporal_activities  # type: ignore[union-attr]
    }
    specs = [
        *queue_specs("agents", Settings(), ObjectStore(object(), "b"), PROVIDERS),  # type: ignore[arg-type]
        *queue_specs("media", Settings(), STORE, PROVIDERS),  # type: ignore[arg-type]
    ]
    served = set()
    for spec in specs:
        for fn in spec.activities:
            served.add((fn.__temporal_activity_definition.name, spec.name))  # type: ignore[attr-defined]
        if spec.plugins:
            served |= {(name, spec.name) for name in agent_names}
    return served


# Runs that rendered each round and reviewed the render, and runs reviewed before the one
# render: the workflow asks for both shapes, so production serves both.
RECORDINGS = sorted(
    path
    for phase in ("phase-08", "review-before-render")
    for path in (Path(__file__).parents[1] / "replay" / "histories" / phase).glob("*.json")
)


@pytest.mark.parametrize("history", RECORDINGS, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_every_activity_the_workflow_schedules_is_served_on_its_queue_by_a_real_worker(
    history: Path,
) -> None:
    """A recording is made with stand-ins for the activities, so it says nothing about
    them; what it does say is every (activity, queue) the workflow asks for. Each must be
    registered, on that queue, by the worker a deployment runs: a step served only by a
    stand-in would pass every test above and leave a real run waiting for ever."""
    asked = _asked_for(history)
    assert asked, "the recording schedules nothing: this would prove nothing"
    assert asked - _served_by_production() == set()


def test_the_recordings_ask_for_the_render_on_the_queue_it_is_served_on() -> None:
    """The render is asked for by name on the `render` queue; the media worker serves it
    there under the same name, so the recorded histories hold as they are."""
    assert {pair for h in RECORDINGS for pair in _asked_for(h)} >= {("render", "render")}
    assert ("render", "render") in _served_by_production()


def test_no_stand_in_is_registered_by_a_production_worker() -> None:
    """The fakes live under tests/: nothing a worker registers comes from there."""
    for spec in [
        *queue_specs("agents", Settings(), ObjectStore(object(), "b"), PROVIDERS),  # type: ignore[arg-type]
        *queue_specs("media", Settings(), STORE, PROVIDERS),  # type: ignore[arg-type]
    ]:
        for fn in spec.activities:
            module = getattr(getattr(fn, "__self__", None), "__module__", fn.__module__)
            assert module.startswith("localization."), (spec.name, fn, module)


def test_each_model_request_of_the_one_request_agents_has_its_own_stage_name() -> None:
    """The stage of a `llm_calls` row is the name of the activity that made the request, so
    the answer and the repair, of the Localizer and of the Dubber, are told apart there and
    from the tool loop's own requests."""
    served = _registered(Settings())
    stages = {
        "agent__localizer_single__model_request",
        "agent__localizer_repair__model_request",
        "agent__dubber_single__model_request",
        "agent__dubber_repair__model_request",
        "agent__localizer__model_request",
        "agent__dubber__model_request",
    }
    assert stages <= served["llm"]
    assert len(stages) == 6
    assert {"localizer_context", "dubber_context", "dubber_setup"} <= served["io"]
    assert not {"localizer_context", "dubber_context", "dubber_setup"} & served["llm"]


def test_the_dubber_that_writes_code_has_its_activities_on_the_agents_queues() -> None:
    """Its model requests on `llm`, and its tool calls on `io` (the reads) and `tts` (the
    speech): a worker missing one of them would leave the run waiting on that queue."""
    specs = runnable(queue_specs("agents", Settings(), ObjectStore(object(), "b"), PROVIDERS))  # type: ignore[arg-type]
    served: dict[str, set[str]] = {}
    for spec in specs:
        activities = [a for plugin in spec.plugins for a in plugin.activities or ()]
        served[spec.name] = {a.__temporal_activity_definition.name for a in activities}
    assert "agent__dubber_code__model_request" in served["llm"]
    tool = "agent__dubber_code__toolset__<agent>__call_tool"
    assert tool in served["io"] and tool in served["tts"]


def test_the_agents_answer_in_one_request_unless_the_setting_says_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (Settings().localizer_mode, Settings().dubber_mode) == ("single", "single")
    monkeypatch.setenv("LOCALIZER_MODE", "tools")
    monkeypatch.setenv("DUBBER_MODE", "tools")
    assert (Settings().localizer_mode, Settings().dubber_mode) == ("tools", "tools")
    monkeypatch.setenv("DUBBER_MODE", "both")
    with pytest.raises(ValueError):
        Settings()


@pytest.mark.parametrize('provider,chunked',[('voicestudio',True),('openai_compat',False)])
def test_voice_studio_worker_enables_speech_chunking(provider,chunked):
    specs=queue_specs('agents',Settings(voice_provider=provider),ObjectStore(object(),'b'),PROVIDERS)
    transcribe=next(fn for spec in specs for fn in spec.activities
                    if fn.__temporal_activity_definition.name=='transcribe')
    assert transcribe.__self__._chunked is chunked


def test_review_project_activities_are_served_on_io():
    assert {"capture_review_source", "build_review_project"} <= _registered(Settings())["io"]


def test_the_voice_role_serves_only_the_speech_queues() -> None:
    from localization.workers.run import VOICE_QUEUES, split_voice_queues

    specs = runnable(queue_specs("agents", Settings(), STORE, PROVIDERS))  # type: ignore[arg-type]
    voice = split_voice_queues("voice", specs, True)
    assert {s.name for s in voice} == VOICE_QUEUES
    assert {s.name for s in voice} == {"stt", "tts"}


def test_the_agents_role_leaves_the_speech_queues_to_the_host_worker_only_when_told_to() -> None:
    from localization.workers.run import split_voice_queues

    specs = runnable(queue_specs("agents", Settings(), STORE, PROVIDERS))  # type: ignore[arg-type]
    everything = {s.name for s in specs}
    assert {"stt", "tts"} <= everything
    assert {s.name for s in split_voice_queues("agents", specs, False)} == everything
    kept = {s.name for s in split_voice_queues("agents", specs, True)}
    assert kept == everything - {"stt", "tts"}
    # Every queue is served by exactly one of the two processes.
    both = [s.name for s in split_voice_queues("agents", specs, True)] + [
        s.name for s in split_voice_queues("voice", specs, True)
    ]
    assert sorted(both) == sorted(everything)


def test_the_media_role_is_never_split() -> None:
    from localization.workers.run import QueueSpec, split_voice_queues

    specs = [QueueSpec("media", 1), QueueSpec("render", 1)]
    assert split_voice_queues("media", specs, True) == specs
