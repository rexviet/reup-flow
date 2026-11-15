"""What the production workers serve for the Vbee dubbing workflow: every activity and
agent it schedules, on the queue it sends them to; the Vbee providers built from settings
and handed to the activities that speak and hear; and a worker that refuses to start in
the Vbee pipeline without what that pipeline needs."""

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from localization.agents import temporal as temporal_module
from localization.config import ConfigError, Settings
from localization.contracts.vbee_flow import VBEE_CONFIGURED_VOICE
from localization.contracts.voice import VbeeSettings, VoiceRef
from localization.providers.registry import build_providers
from localization.providers.tts_router import TtsByProvider
from localization.providers.vbee import VbeeStt, VbeeTts
from localization.storage.object_store import ObjectStore
from localization.workers.run import queue_specs, runnable
from localization.workflows import vbee_dubbing
from localization.workflows.revoice_caption import RevoiceCaptionWorkflow
from localization.workflows.vbee_dubbing import VbeeDubbingWorkflow
from localization.workflows.video_localization import INGEST_QUEUE, VideoLocalizationWorkflow
from tests.support.tts_fakes import FakeTts

STORE = ObjectStore(object(), "b")
CANARY = "vbee-voice-wiring-canary"
MARKED = VoiceRef(provider="vbee", voice_id=VBEE_CONFIGURED_VOICE, settings=VbeeSettings())


def providers(**vbee: object) -> SimpleNamespace:
    return SimpleNamespace(
        stt=object(),
        ocr=object(),
        tts=FakeTts(),
        demucs=object(),
        vbee_stt=vbee.get("stt"),
        vbee_tts=vbee.get("tts"),
        zerotts_tts=None,
        hear=None,
        singing=None,
    )


def served(settings: Settings, given: SimpleNamespace | None = None) -> dict[str, set[str]]:
    """Activity names each queue's worker serves, on both workers, counting the plugins."""
    out: dict[str, set[str]] = {}
    for role in ("agents", "media", "ingest"):
        for spec in runnable(queue_specs(role, settings, STORE, given or providers())):  # type: ignore[arg-type]
            names = {fn.__temporal_activity_definition.name for fn in spec.activities}  # type: ignore[attr-defined]
            for plugin in spec.plugins:
                names |= {fn.__temporal_activity_definition.name for fn in plugin.activities}
            out[spec.name] = names
    return out


# Every activity the workflow schedules and the queue it sends it to, read from the
# workflow's own configuration (and the workflow test checks the same table in a run).
SENT = {
    "ingest": {"task_queue": INGEST_QUEUE},
    "detect_subtitles": vbee_dubbing.DETECT_SUBTITLES,
    "voice_rate": vbee_dubbing.VOICE_RATE,
    "vbee_stt": vbee_dubbing.VBEE_STT,
    "plan_slots": vbee_dubbing.PLAN_SLOTS,
    "write_slot_script": vbee_dubbing.DUB_IO,
    "plan_synthesis": vbee_dubbing.DUB_IO,
    "synthesize_segments": vbee_dubbing.SYNTHESIZE,
    "clip_durations": vbee_dubbing.DUB_IO,
    "commit_dub": vbee_dubbing.DUB_IO,
    "assemble_audio": vbee_dubbing.ASSEMBLE_AUDIO,
    "layout_captions": vbee_dubbing.LAYOUT,
    "review_dub": vbee_dubbing.REVIEW_DUB,
    "render": vbee_dubbing.RENDER,
    "publish": vbee_dubbing.PUBLISH,
}


@pytest.mark.parametrize("name", sorted(SENT))
def test_each_activity_of_the_workflow_is_served_on_the_queue_it_is_sent_to(name: str) -> None:
    queues = served(Settings())
    queue = SENT[name]["task_queue"]
    assert name in queues[queue], (name, queue)


def test_the_shortener_agents_are_registered_on_the_workflow_and_served_on_llm() -> None:
    agents = VbeeDubbingWorkflow.__pydantic_ai_agents__
    assert agents == [
        temporal_module.slot_shortener_agent,
        temporal_module.slot_shortener_repair_agent,
    ]
    queues = served(Settings())
    assert {
        "agent__slot_shortener__model_request",
        "agent__slot_shortener_repair__model_request",
    } <= queues["llm"]


def test_all_workflows_are_served_on_the_workflows_queue() -> None:
    (spec,) = [
        s
        for s in queue_specs("agents", Settings(), STORE, providers())
        if s.name == "workflows"  # type: ignore[arg-type]
    ]
    assert list(spec.workflows) == [
        VideoLocalizationWorkflow,
        VbeeDubbingWorkflow,
        RevoiceCaptionWorkflow,
    ]


def _activity(specs, name: str):  # noqa: ANN001, ANN202
    return next(
        fn
        for s in specs
        for fn in s.activities
        if fn.__temporal_activity_definition.name == name  # type: ignore[attr-defined]
    )


async def test_the_speaking_activities_route_a_marked_voice_to_vbee_in_the_configured_voice() -> (
    None
):
    vbee, studio = FakeTts(), FakeTts()
    given = SimpleNamespace(**{**vars(providers(tts=vbee)), "tts": studio})
    settings = Settings(vbee_voice_id=SecretStr(CANARY))
    specs = queue_specs("agents", settings, STORE, given)  # type: ignore[arg-type]
    synthesis = _activity(specs, "synthesize_segments").__self__
    synthesizer = synthesis.synthesizer
    # The provider gets the configured voice; the old pipeline's voice goes to VoiceStudio.
    clip_voice = synthesizer._resolve(MARKED)  # noqa: SLF001
    assert clip_voice.voice_id == CANARY
    tts = synthesizer._tts  # noqa: SLF001
    assert isinstance(tts, TtsByProvider)
    await tts.synthesize("xin chào", clip_voice)
    await tts.synthesize(
        "tạm biệt",
        VoiceRef.model_validate({"provider": "voicestudio", "voice_id": "v", "settings": {}}),
    )
    assert (vbee.voices, studio.voices) == ([CANARY], ["v"])
    rate = _activity(specs, "voice_rate").__self__
    assert rate._tts is tts or isinstance(rate._tts, TtsByProvider)  # noqa: SLF001
    assert rate._resolve(MARKED).voice_id == CANARY  # noqa: SLF001
    hear = _activity(specs, "vbee_stt").__self__
    assert hear._stt is given.vbee_stt  # noqa: SLF001


def vbee_settings(**more: object) -> Settings:
    values: dict[str, object] = {
        "localization_pipeline": "vbee",
        "vbee_app_id": SecretStr("app"),
        "vbee_api_token": SecretStr("token"),
        "vbee_voice_id": SecretStr(CANARY),
        "openrouter_api_key": SecretStr("k"),
        "ocr_model": "some/ocr",
        "ocr_bbox_format": "xyxy_px",
    }
    values.update(more)
    return Settings(**values)  # type: ignore[arg-type]


def test_the_vbee_pipeline_builds_vbee_and_needs_no_voicestudio() -> None:
    built = build_providers(vbee_settings())
    assert isinstance(built.vbee_stt, VbeeStt) and isinstance(built.vbee_tts, VbeeTts)
    assert built.stt is None and built.tts is None


@pytest.mark.parametrize("missing", ["vbee_app_id", "vbee_api_token", "vbee_voice_id"])
def test_the_vbee_pipeline_refuses_to_start_without_a_vbee_setting(missing: str) -> None:
    with pytest.raises(ConfigError, match="LOCALIZATION_PIPELINE=vbee"):
        build_providers(vbee_settings(**{missing: None}))


def test_the_voicestudio_pipeline_builds_no_vbee_without_its_credentials() -> None:
    settings = Settings(
        voicestudio_url="https://vs.example",
        stt_model="s",
        tts_model="t",
        openrouter_api_key=SecretStr("k"),
        ocr_model="some/ocr",
        ocr_bbox_format="xyxy_px",
    )
    built = build_providers(settings)
    assert built.vbee_stt is None and built.vbee_tts is None
    assert built.stt is not None and built.tts is not None
