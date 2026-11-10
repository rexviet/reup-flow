import hashlib

import pytest
from temporalio.testing import ActivityEnvironment

from localization.activities.ffmpeg import probe_duration_ms
from localization.review_bridge.models import SaveCue
from tests.support.audio import RATE, decode, level, tone_span_ms
from tests.support.revoice import audio_project, reserve


@pytest.mark.parametrize("blank", [False, True])
async def test_real_atempo_changes_only_selected_clip_and_mixed_audio(
    review_store, tmp_path, blank
):
    service, acts, provider = audio_project(review_store, tmp_path, blank=blank)
    repo = service.repo
    original = repo.current("7001", 1)[1]
    key = reserve(service)
    env = ActivityEnvironment()
    clip_ref = await env.run(acts.synthesize, key)
    assert await env.run(acts.synthesize, key) == clip_ref
    candidate_ref = await env.run(acts.mix, key, clip_ref)
    result = await env.run(acts.commit, key, candidate_ref)
    assert result["status"] == "completed"
    assert await env.run(acts.commit, key, candidate_ref) == result
    assert provider.calls == [("Giọng mới", "voicestudio")]
    current = repo.current("7001", 1)[1]
    assert current.cues[1] == original.cues[1]
    assert current.cues[0].clip != original.cues[0].clip
    assert current.cues[0].stretch_ratio == pytest.approx(1.1)
    assert current.mixed_audio != original.mixed_audio
    path = tmp_path / "audible.wav"
    path.write_bytes(repo.read_asset(current.mixed_audio))
    assert abs(probe_duration_ms(path) - 2000) <= 50
    samples = decode(path)
    first, last = tone_span_ms(samples[: 1100 * RATE // 1000], 440, floor=0.015)
    assert first < 40 and 900 <= last <= 1040  # 1.1 s is actually played in 1.0 s
    assert level(samples, 1100, 1300, 440) < 0.001
    assert level(samples, 1450, 1850, 220) > 0.05
    source = repo.read_asset(current.html).decode()
    assert source.count("<audio ") == 1 and "muted playsinline" in source


@pytest.mark.parametrize("duration", [1.151, 2.5])
async def test_overflow_keeps_caption_and_active_clip(review_store, tmp_path, duration):
    service, acts, _ = audio_project(review_store, tmp_path, duration=duration)
    before = service.repo.current("7001", 1)[1]
    key = reserve(service)
    env = ActivityEnvironment()
    clip = await env.run(acts.synthesize, key)
    candidate = await env.run(acts.mix, key, clip)
    assert (await env.run(acts.commit, key, candidate))["status"] == "overflow"
    assert service.repo.current("7001", 1)[1] == before
    assert service.project("7001", 1)["cues"][0]["mismatch"]


async def test_late_result_never_overwrites_newer_caption(review_store, tmp_path):
    service, acts, _ = audio_project(review_store, tmp_path)
    key = reserve(service)
    env = ActivityEnvironment()
    candidate = await env.run(acts.mix, key, await env.run(acts.synthesize, key))
    state = service.project("7001", 1)
    service.save(
        "7001",
        1,
        17,
        SaveCue(
            expectedRevision=state["revision"],
            headETag=state["headETag"],
            text="Thay đổi sau khi tạo giọng",
            start_ms=0,
            end_ms=1000,
        ),
    )
    before = service.repo.current("7001", 1)[1]
    result = await env.run(acts.commit, key, candidate)
    assert result["status"] == "conflict" and result["candidateRevision"]
    assert service.repo.current("7001", 1)[1] == before


async def test_crash_after_head_cas_recovers_even_after_later_save(
    review_store, tmp_path, monkeypatch
):
    service, acts, provider = audio_project(review_store, tmp_path)
    key = reserve(service)
    env = ActivityEnvironment()
    candidate = await env.run(acts.mix, key, await env.run(acts.synthesize, key))
    original = acts._result
    with monkeypatch.context() as patch:

        def crash(*args):
            raise RuntimeError("after HEAD CAS")

        patch.setattr(acts, "_result", crash)
        with pytest.raises(RuntimeError, match="after HEAD CAS"):
            await env.run(acts.commit, key, candidate)
    state = service.project("7001", 1)
    service.save(
        "7001",
        1,
        17,
        SaveCue(
            expectedRevision=state["revision"],
            headETag=state["headETag"],
            text="Bản sau",
            start_ms=0,
            end_ms=1000,
        ),
    )
    result = await env.run(acts.commit, key, candidate)
    assert result["status"] == "completed"
    assert result["revision"] == hashlib.sha256(key.encode()).hexdigest()[:32]
    assert service.repo.current("7001", 1)[1].cues[0].text_vi == "Bản sau"
    assert len(provider.calls) == 1
    assert original(key, result) == result


async def test_foreign_clip_ref_is_rejected(review_store, tmp_path):
    service, acts, _ = audio_project(review_store, tmp_path)
    with pytest.raises(ValueError, match="foreign"):
        await ActivityEnvironment().run(acts.mix, reserve(service), "review/other/clip.json")


async def test_revoice_uses_existing_vbee_resolver_and_provider_routing(review_store, tmp_path):
    from pydantic import SecretStr

    from localization.activities.revoice_caption import RevoiceActivities
    from localization.activities.synthesize import Synthesizer
    from localization.contracts.vbee_flow import VBEE_CONFIGURED_VOICE
    from localization.contracts.voice import VbeeSettings, VoiceRef
    from localization.providers.tts_router import ConfiguredVoices, TtsByProvider
    from tests.support.revoice import CueTts

    service, _, default = audio_project(review_store, tmp_path)
    repo = service.repo
    _, base, etag = repo.current("7001", 1)
    base = base.model_copy(
        update={
            "voice": VoiceRef(
                provider="vbee",
                voice_id=VBEE_CONFIGURED_VOICE,
                settings=VbeeSettings(),
            )
        }
    )
    repo.activate(repo.prepare(base, base.cues, {}), etag)
    repo.materialize("7001", 1)
    vbee = CueTts(duration=0.8)
    synth = Synthesizer(
        review_store,
        TtsByProvider(default, vbee),
        model="tts-model",
        resolve=ConfiguredVoices(SecretStr("private-test-voice")).resolve,
    )
    activities = RevoiceActivities(review_store, synth)
    key = reserve(service)
    env = ActivityEnvironment()
    clip = await env.run(activities.synthesize, key)
    candidate = await env.run(activities.mix, key, clip)
    assert (await env.run(activities.commit, key, candidate))["status"] == "completed"
    assert vbee.calls == [("Giọng mới", "vbee")] and not default.calls
    assert "private-test-voice" not in repo.read_json(key)["hash"]
