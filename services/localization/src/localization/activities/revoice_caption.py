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
from localization.activities.ingest import PREP_ROUND
from localization.activities.synthesize import background_heartbeat, wav_duration_ms
from localization.contracts import LocalizedScript, ReviewAsset, ReviewProjectManifest
from localization.review_bridge.repository import Conflict, Repository
from localization.review_bridge.service import batch_hash, request_hash
from localization.review_bridge.visual import capture_visual
from localization.storage.object_store import ArtifactMissingError, StorageConflictError

# The reference `revoice_synthesize` returns for a blank caption: the mix drops that cue's
# clip instead of placing a new one. The workflow passes it on unchanged.
REMOVE_SUFFIX = ".remove"
# A new voice may run this much longer than its line's slot, sped up to fit.
MAX_STRETCH = 1.15


class RevoiceActivities:
    def __init__(
        self, store, synthesizer=None, *, target=-14.0, peak=-1.5, original_voice_gain=0.0
    ):
        self.repo = Repository(store)
        self.synth = synthesizer
        self.target = target
        self.peak = peak
        self.under = original_voice_gain

    def original_vocals(self, base, path):
        """Writes the project's original vocals to `path`; False when it has none. A
        project made before it kept them reads the stems its run separated."""
        if base.vocals is not None:
            path.write_bytes(self.repo.read_asset(base.vocals))
            return True
        key = f"work/{base.aweme_id}/g{base.generation}/r{PREP_ROUND}/vocals.wav"
        try:
            self.repo.store.download_file(key, path, produced_by="ingest")
        except ArtifactMissingError:
            return False
        return True

    def request(self, key):
        """A single-line request: the request, its base revision and its line."""
        request, base, cues = self.requested(key)
        if "cueIds" in request["command"]:
            raise ValueError("a batch request names several lines")
        return request, base, cues[0]

    def requested(self, key):
        """Any request: the request, its base revision and the lines it revoices."""
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
        by_id = {c.id: c for c in base.cues}
        if "cueIds" in command:
            cues = tuple(by_id[i] for i in command["cueIds"])
            digest = batch_hash(base, cues)
        else:
            cues = (by_id[command["cueId"]],)
            digest = request_hash(base, cues[0])
        if request["hash"] != digest:
            raise ValueError("revoice request hash mismatch")
        return request, base, cues

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

    def clip_refs(self, request_key, cue_id=None):
        """Where a line's new voice is recorded, and the reference that drops its voice.
        A single-line request has one line and names no line in them."""
        stem = "" if cue_id is None else f".cue-{cue_id}"
        return (
            self.derived_key(request_key, f"{stem}.clip.json"),
            self.derived_key(request_key, f"{stem}{REMOVE_SUFFIX}"),
        )

    @activity.defn(name="revoice_synthesize")
    async def synthesize(self, request_key: str) -> str:
        _, base, cue = await asyncio.to_thread(self.request, request_key)
        return await self._synthesize(base, cue, *self.clip_refs(request_key))

    @activity.defn(name="revoice_synthesize_cue")
    async def synthesize_cue(self, request_key: str, cue_id: int) -> str:
        _, base, cues = await asyncio.to_thread(self.requested, request_key)
        cue = next((c for c in cues if c.id == cue_id), None)
        if cue is None:
            raise ValueError("the request does not revoice this line")
        return await self._synthesize(base, cue, *self.clip_refs(request_key, cue_id))

    async def _synthesize(self, base, cue, key, remove):
        if await asyncio.to_thread(self.repo.optional_json, key) is not None:
            return key
        if not cue.text_vi.strip():
            # A blank caption is spoken by nobody: the mix drops its clip, no TTS is asked.
            return remove
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
        if clip_ref not in self.clip_refs(request_key):
            raise ValueError("foreign clip reference")
        return await run_blocking(self._mix, request_key, clip_ref)

    def _mix(self, request_key, clip_ref):
        request, base, cue = self.request(request_key)
        candidate_key = self.candidate_key(request_key, base)
        if self.prepared(request_key, base):
            return candidate_key
        changed = self.revoiced(cue, clip_ref, self.clip_refs(request_key)[1])
        if changed is None:
            return self.derived_key(request_key, ".overflow")
        self._remix(request_key, request, base, {cue.id: changed})
        return candidate_key

    @activity.defn(name="revoice_mix_batch")
    async def mix_batch(self, request_key: str, clip_refs: list[str]) -> dict:
        """`{"candidate": the new revision's manifest, or None when no line fits,
        "overflow": the lines whose new voice is too long and keep their old one}`."""
        return await run_blocking(self._mix_batch, request_key, clip_refs)

    def _mix_batch(self, request_key, clip_refs):
        request, base, cues = self.requested(request_key)
        if len(clip_refs) != len(cues):
            raise ValueError("one clip reference per line")
        changes, overflow = {}, []
        for cue, ref in zip(cues, clip_refs, strict=True):
            if ref not in self.clip_refs(request_key, cue.id):
                raise ValueError("foreign clip reference")
            changed = self.revoiced(cue, ref, self.clip_refs(request_key, cue.id)[1])
            if changed is None:
                overflow.append(cue.id)
            else:
                changes[cue.id] = changed
        if not changes:
            return {"candidate": None, "overflow": overflow}
        if not self.prepared(request_key, base):
            self._remix(request_key, request, base, changes)
        return {"candidate": self.candidate_key(request_key, base), "overflow": overflow}

    def candidate_key(self, request_key, base):
        revision = self.revision_for(request_key)
        return f"review/{base.aweme_id}/g{base.generation}/r{revision}/manifest.json"

    def prepared(self, request_key, base):
        """Whether an earlier attempt already wrote this request's new revision."""
        try:
            self.repo.manifest(base.aweme_id, base.generation, self.revision_for(request_key))
            return True
        except ArtifactMissingError:
            return False

    def revoiced(self, cue, clip_ref, remove):
        """The line as its new voice makes it: without a voice for a removal, None when
        the new voice is too long for its slot."""
        if clip_ref == remove:
            if cue.text_vi.strip():
                raise ValueError("only a blank caption has its voice removed")
            return cue.model_copy(
                update={
                    "clip": None,
                    "spoken_text": "",
                    "audio_start_ms": cue.start_ms,
                    "audio_duration_ms": 0,
                    "stretch_ratio": 1.0,
                    "overflow_unresolved": False,
                }
            )
        record = self.repo.read_json(clip_ref)
        clip = ReviewAsset.model_validate(record["asset"])
        duration = record["duration_ms"]
        slot = cue.end_ms - cue.start_ms
        if slot <= 0 or duration > slot * MAX_STRETCH:
            return None
        return cue.model_copy(
            update={
                "clip": clip,
                "spoken_text": cue.text_vi,
                "audio_start_ms": cue.start_ms,
                "audio_duration_ms": duration,
                "stretch_ratio": max(1.0, duration / slot),
                "overflow_unresolved": False,
            }
        )

    def _remix(self, request_key, request, base, changes):
        """Mixes the project with `changes` (line id to the line with its new voice) and
        writes the revision that holds them; `revoice_commit` makes it current."""
        cues = tuple(changes.get(c.id, c) for c in base.cues)
        revision = self.revision_for(request_key)
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
                new = changes.get(item.id)
                if new is not None and probe_duration_ms(path) != new.audio_duration_ms:
                    raise ValueError("clip duration mismatch")
                clips.append(path)
                placements.append((item.audio_start_ms, item.stretch_ratio))
            # Mixed as the first mix was: the original voice quietly under the dub, and
            # at full level over the sung lines, which nobody speaks over.
            script = LocalizedScript.model_validate_json(self.repo.read_asset(base.script))
            kept = [(s.start_ms, s.end_ms) for s in script.segments if s.keep_original]
            vocals = root / "vocals.wav"
            if not self.original_vocals(base, vocals):
                vocals, kept = None, []
            out = root / "dubbed.wav"
            levels = render(
                bgm=bgm,
                clips=clips,
                placements=placements,
                duration_ms=base.duration_ms,
                out=out,
                work=root,
                target=self.target,
                peak=self.peak,
                allow_silence=True,
                vocals=vocals,
                kept=kept,
                under=self.under if vocals is not None else 0.0,
                # The collection's offset, as the first mix of the project had it.
                music_offset_db=base.bgm_offset_db or 0.0,
                # The levels the project was first mixed with: one pass instead of five.
                # A project without them is measured once here and keeps them.
                levels=base.mix_levels,
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
            levels=levels,
        )

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

    @activity.defn(name="revoice_commit_batch")
    async def commit_batch(self, request_key: str, mixed: dict) -> dict:
        """Makes the batch's revision current; the lines whose new voice was too long are
        reported, and keep their old voice. When none fit, nothing changes."""
        overflow = {"overflowCues": list(mixed["overflow"])}
        candidate = mixed["candidate"] or self.derived_key(request_key, ".overflow")
        return await asyncio.to_thread(self._commit, request_key, candidate, **overflow)

    def _commit(self, request_key, candidate_ref, **details):
        known = self.repo.optional_json(self.derived_key(request_key, ".result.json"))
        if known is not None:
            return known
        request, base, _ = self.requested(request_key)
        if candidate_ref == self.derived_key(request_key, ".overflow"):
            result = self.result_for(request, base.revision_id, "overflow", **details)
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
            self.result_for(request, revision, "completed", **details),
        )

    @activity.defn(name="revoice_failed")
    async def failed(self, request_key: str) -> dict:
        def finish():
            request, base, _ = self.requested(request_key)
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
