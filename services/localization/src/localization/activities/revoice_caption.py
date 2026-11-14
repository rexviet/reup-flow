"""Worker-owned TTS, measured mixing and idempotent revision activation."""

import asyncio
import hashlib
import json
import re
import tempfile
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from localization.activities.assemble_audio import check_length, render
from localization.activities.ffmpeg import probe_duration_ms, run_blocking
from localization.activities.synthesize import background_heartbeat, wav_duration_ms
from localization.contracts import ReviewAsset, ReviewProjectManifest
from localization.review_bridge.repository import Conflict, Repository
from localization.review_bridge.service import request_hash
from localization.review_bridge.visual import capture_visual
from localization.storage.object_store import ArtifactMissingError, StorageConflictError

# The reference `revoice_synthesize` returns for a blank caption: the mix drops that cue's
# clip instead of placing a new one. The workflow passes it on unchanged.
REMOVE_SUFFIX = ".remove"


class RevoiceActivities:
    def __init__(self, store, synthesizer=None, *, target=-14.0, peak=-1.5):
        self.repo = Repository(store)
        self.synth = synthesizer
        self.target = target
        self.peak = peak

    def request(self, key):
        pattern = r"review/[0-9]{1,32}/g[0-9]+/requests/[A-Za-z0-9_-]{16,64}\.json"
        if re.fullmatch(pattern, key) is None:
            raise ValueError("invalid revoice request reference")
        request = self.repo.read_json(key)
        command = request["command"]
        expected = self.repo.request_key(
            request["aweme_id"], request["generation"], command["requestId"]
        )
        if expected != key:
            raise ValueError("request identity mismatch")
        base = self.repo.manifest(
            request["aweme_id"], request["generation"], command["baseRevision"]
        )
        cue = next(c for c in base.cues if c.id == command["cueId"])
        if request["hash"] != request_hash(base, cue):
            raise ValueError("revoice request hash mismatch")
        return request, base, cue

    def derived_key(self, key, suffix):
        return key.removesuffix(".json") + suffix

    def revision_for(self, request_key):
        return hashlib.sha256(request_key.encode()).hexdigest()[:32]

    def result_for(self, request, revision, status, **details):
        return {
            "status": status,
            "revision": revision,
            "requestId": request["command"]["requestId"],
            **details,
        }

    @activity.defn(name="revoice_synthesize")
    async def synthesize(self, request_key: str) -> str:
        key = self.derived_key(request_key, ".clip.json")
        if await asyncio.to_thread(self.repo.optional_json, key) is not None:
            return key
        _, base, cue = await asyncio.to_thread(self.request, request_key)
        if not cue.text_vi.strip():
            # A blank caption is spoken by nobody: the mix drops its clip, no TTS is asked.
            return self.derived_key(request_key, REMOVE_SUFFIX)
        if self.synth is None or self.synth._model != base.model:
            raise ApplicationError("TTS model changed", non_retryable=True)
        async with background_heartbeat():
            clip = await self.synth.clip(base.voice, base.engine, cue.text_vi)
            data = await asyncio.to_thread(
                self.repo.store.get_bytes, clip.key, produced_by="synthesize"
            )
            duration = await run_blocking(wav_duration_ms, data)
            if duration <= 0:
                raise ApplicationError("Empty speech clip", non_retryable=True)
            asset = await asyncio.to_thread(self.repo.asset, base, data, ".wav")
            record = {"asset": asset.model_dump(), "duration_ms": duration}
            try:
                await asyncio.to_thread(
                    self.repo.store.put_immutable_bytes,
                    key,
                    json.dumps(record, sort_keys=True).encode(),
                )
            except StorageConflictError:
                await asyncio.to_thread(self.repo.read_json, key)
        return key

    @activity.defn(name="revoice_mix")
    async def mix(self, request_key: str, clip_ref: str) -> str:
        allowed = {
            self.derived_key(request_key, ".clip.json"),
            self.derived_key(request_key, REMOVE_SUFFIX),
        }
        if clip_ref not in allowed:
            raise ValueError("foreign clip reference")
        return await run_blocking(self._mix, request_key, clip_ref)

    def _mix(self, request_key, clip_ref):
        request, base, cue = self.request(request_key)
        revision = self.revision_for(request_key)
        candidate_key = f"review/{base.aweme_id}/g{base.generation}/r{revision}/manifest.json"
        try:
            self.repo.manifest(base.aweme_id, base.generation, revision)
            return candidate_key
        except ArtifactMissingError:
            pass
        if clip_ref == self.derived_key(request_key, REMOVE_SUFFIX):
            if cue.text_vi.strip():
                raise ValueError("only a blank caption has its voice removed")
            clip, duration = None, None
            changed = cue.model_copy(
                update={
                    "clip": None,
                    "spoken_text": "",
                    "audio_start_ms": cue.start_ms,
                    "audio_duration_ms": 0,
                    "stretch_ratio": 1.0,
                    "overflow_unresolved": False,
                }
            )
        else:
            record = self.repo.read_json(clip_ref)
            clip = ReviewAsset.model_validate(record["asset"])
            duration = record["duration_ms"]
            slot = cue.end_ms - cue.start_ms
            if slot <= 0 or duration > slot * 1.15:
                return self.derived_key(request_key, ".overflow")
            changed = cue.model_copy(
                update={
                    "clip": clip,
                    "spoken_text": cue.text_vi,
                    "audio_start_ms": cue.start_ms,
                    "audio_duration_ms": duration,
                    "stretch_ratio": max(1.0, duration / slot),
                    "overflow_unresolved": False,
                }
            )
        cues = tuple(changed if c.id == cue.id else c for c in base.cues)
        with tempfile.TemporaryDirectory(prefix="revoice-") as directory:
            root = Path(directory)
            bgm = root / "bgm.wav"
            bgm.write_bytes(self.repo.read_asset(base.bgm))
            clips, placements = [], []
            for item in cues:
                if item.clip is None:
                    continue
                path = root / f"cue-{item.id}.wav"
                path.write_bytes(self.repo.read_asset(item.clip))
                if item.id == cue.id and clip is not None and probe_duration_ms(path) != duration:
                    raise ValueError("clip duration mismatch")
                clips.append(path)
                placements.append((item.audio_start_ms, item.stretch_ratio))
            out = root / "dubbed.wav"
            render(
                bgm=bgm,
                clips=clips,
                placements=placements,
                duration_ms=base.duration_ms,
                out=out,
                work=root,
                target=self.target,
                peak=self.peak,
                allow_silence=True,
                # The collection's offset, as the first mix of the project had it.
                music_offset_db=base.bgm_offset_db or 0.0,
            )
            check_length(out, base.duration_ms)
            mixed = self.repo.asset(base, out.read_bytes(), ".wav")
        self.repo.prepare(
            base,
            cues,
            capture_visual(self.repo.read_asset(base.html), base.cues),
            revision=revision,
            operation=request["command"]["requestId"],
            mixed=mixed,
        )
        return candidate_key

    def _result(self, request_key, result):
        key = self.derived_key(request_key, ".result.json")
        try:
            self.repo.store.put_immutable_bytes(key, json.dumps(result, sort_keys=True).encode())
        except StorageConflictError:
            return self.repo.read_json(key)
        return result

    @activity.defn(name="revoice_commit")
    async def commit(self, request_key: str, candidate_ref: str) -> dict:
        return await asyncio.to_thread(self._commit, request_key, candidate_ref)

    def _commit(self, request_key, candidate_ref):
        known = self.repo.optional_json(self.derived_key(request_key, ".result.json"))
        if known is not None:
            return known
        request, base, _ = self.request(request_key)
        if candidate_ref == self.derived_key(request_key, ".overflow"):
            result = self.result_for(request, base.revision_id, "overflow")
            return self._result(request_key, result)
        revision = self.revision_for(request_key)
        expected = f"review/{base.aweme_id}/g{base.generation}/r{revision}/manifest.json"
        if candidate_ref != expected:
            raise ValueError("foreign candidate reference")
        candidate = ReviewProjectManifest.model_validate_json(
            self.repo.store.get_bytes(candidate_ref, produced_by="revoice_mix")
        )
        if (
            candidate.parent_revision_id != base.revision_id
            or candidate.operation_id != request["command"]["requestId"]
        ):
            raise ValueError("candidate provenance mismatch")
        try:
            self.repo.activate(candidate, request["etag"])
        except Conflict as exc:
            result = self.result_for(
                request,
                exc.revision,
                "conflict",
                candidateRevision=revision,
            )
            return self._result(
                request_key,
                result,
            )
        return self._result(
            request_key,
            self.result_for(request, revision, "completed"),
        )

    @activity.defn(name="revoice_failed")
    async def failed(self, request_key: str) -> dict:
        def finish():
            request, base, _ = self.request(request_key)
            revision = self.revision_for(request_key)
            try:
                candidate = self.repo.manifest(base.aweme_id, base.generation, revision)
                if self.repo.committed(candidate):
                    return self._result(
                        request_key,
                        self.result_for(request, revision, "completed"),
                    )
            except ArtifactMissingError:
                pass
            return self._result(
                request_key,
                self.result_for(request, base.revision_id, "failed"),
            )

        return await asyncio.to_thread(finish)
