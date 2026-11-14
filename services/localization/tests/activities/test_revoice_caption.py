import hashlib

import pytest
from temporalio.testing import ActivityEnvironment

from localization.activities.ffmpeg import probe_duration_ms
from localization.review_bridge.models import SaveCue
from localization.storage.object_store import ObjectStore
from tests.support.audio import RATE, db, decode, level, tone_span_ms
from tests.support.revoice import MUSIC_HZ, audio_project, reserve


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


def save_blank(service):
    state = service.project("7001", 1)
    service.save(
        "7001",
        1,
        17,
        SaveCue(
            expectedRevision=state["revision"],
            headETag=state["headETag"],
            text="",
            start_ms=0,
            end_ms=1000,
        ),
    )


async def test_a_blank_caption_has_its_voice_removed_from_the_mix(review_store, tmp_path):
    """A duplicated line saved blank, then "Xoá giọng": its clip leaves the mix, the
    other cue's stays, and the cue is in sync again."""
    service, acts, provider = audio_project(review_store, tmp_path)
    repo = service.repo
    save_blank(service)
    assert service.project("7001", 1)["cues"][0]["mismatch"]  # blank caption, old voice
    original = repo.current("7001", 1)[1]
    key = reserve(service)
    env = ActivityEnvironment()
    clip_ref = await env.run(acts.synthesize, key)
    assert clip_ref.endswith(".remove") and provider.calls == []
    candidate_ref = await env.run(acts.mix, key, clip_ref)
    assert (await env.run(acts.commit, key, candidate_ref))["status"] == "completed"
    current = repo.current("7001", 1)[1]
    assert current.cues[0].clip is None and current.cues[0].spoken_text == ""
    assert current.cues[1] == original.cues[1]
    assert not service.project("7001", 1)["cues"][0]["mismatch"]
    path = tmp_path / "removed.wav"
    path.write_bytes(repo.read_asset(current.mixed_audio))
    samples = decode(path)
    assert level(samples, 50, 450, 220) < 0.001  # the removed cue is silent
    assert level(samples, 1450, 1850, 220) > 0.05  # the other cue still plays


async def test_a_blank_caption_without_a_voice_has_nothing_to_remove(review_store, tmp_path):
    service, acts, _ = audio_project(review_store, tmp_path, blank=True)
    save_blank(service)
    with pytest.raises(ValueError, match="no voice to remove"):
        reserve(service)


async def test_a_removal_reference_is_refused_for_a_caption_with_text(review_store, tmp_path):
    service, acts, _ = audio_project(review_store, tmp_path)
    key = reserve(service)
    with pytest.raises(ValueError, match="only a blank caption"):
        await ActivityEnvironment().run(acts.mix, key, key.removesuffix(".json") + ".remove")


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


async def _revoiced_music_against_voice(store, tmp_path, offset) -> float:
    """Revoice the first line and measure the music, where nobody speaks, against the
    line after it: loudness normalisation moves both together."""
    settings = (
        {}
        if offset is None
        else {
            "bgm_offset_db": offset,
            "caption_color": "#00ff88",
            "cover_color": "#202020",
            "cover_opacity_pct": 80,
        }
    )
    tmp_path.mkdir()
    service, acts, _ = audio_project(store, tmp_path, music=0.25, settings=settings)
    key = reserve(service)
    env = ActivityEnvironment()
    candidate = await env.run(acts.mix, key, await env.run(acts.synthesize, key))
    assert (await env.run(acts.commit, key, candidate))["status"] == "completed"
    current = service.repo.current("7001", 1)[1]
    assert current.operation_id == "revoice-test-123456"
    assert {k: getattr(current, k) for k in settings} == settings
    if settings:
        # The revoiced revision is drawn as the collection says, as the first was.
        html = service.repo.read_asset(current.html).decode()
        assert "color:#00ff88;" in html
        assert ".source-mask{background:rgba(32,32,32,0.8)}" in html
    path = tmp_path / "revoiced.wav"
    path.write_bytes(service.repo.read_asset(current.mixed_audio))
    samples = decode(path)
    return db(level(samples, 1100, 1350, MUSIC_HZ) / level(samples, 1450, 1850, 220))


async def test_revoicing_a_line_keeps_the_collection_music_offset(review_store, tmp_path):
    client = review_store._client  # noqa: SLF001
    client.create_bucket(Bucket="review-test-plain")
    plain_store = ObjectStore(client, "review-test-plain")
    plain = await _revoiced_music_against_voice(plain_store, tmp_path / "plain", None)
    quieter = await _revoiced_music_against_voice(review_store, tmp_path / "offset", -6.0)
    assert quieter - plain == pytest.approx(-6.0, abs=1.0)
