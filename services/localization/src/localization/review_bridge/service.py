"""Caption saves and idempotent dispatch; durable HEAD remains authoritative."""

import asyncio
import hashlib
import json

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from localization.review_bridge.models import (
    CueEdit,
    RevoiceCue,
    RevoiceCues,
    SaveAndRevoice,
    SaveCue,
    mismatch,
)
from localization.review_bridge.repository import RENDERS, Conflict, atomic_write, safe_path
from localization.review_bridge.visual import capture_visual, require_visual_only
from localization.storage.object_store import ArtifactMissingError, StorageConflictError
from localization.workflows.revoice_caption import RevoiceBatchWorkflow, RevoiceCaptionWorkflow


class QueueFull(Exception):
    pass


class Service:
    def __init__(self, repository, temporal=None):
        self.repo = repository
        self.temporal = temporal
        self._dispatch_lock = asyncio.Lock()

    def project(self, aweme, generation):
        _, project, etag = self.repo.current(aweme, generation)
        return {
            "aweme_id": aweme,
            "generation": generation,
            "revision": project.revision_id,
            "headETag": etag,
            "duration_ms": project.duration_ms,
            "cues": [dict(c.model_dump(mode="json"), mismatch=mismatch(c)) for c in project.cues],
        }

    def save(self, aweme, generation, cue_id, command: SaveCue):
        edit = CueEdit(
            cueId=cue_id, text=command.text, start_ms=command.start_ms, end_ms=command.end_ms
        )
        return self.save_edits(
            aweme, generation, command.expectedRevision, command.headETag, (edit,)
        )

    def save_edits(self, aweme, generation, expected_revision, head_etag, edits):
        """Saves every edit as one revision; nothing is saved if any edit is refused."""
        with self.repo.lock(aweme, generation) as folder:
            _, base, etag = self.repo.current(aweme, generation)
            if (base.revision_id, etag) != (expected_revision, head_etag):
                raise Conflict(base.revision_id)
            by_id = {c.id: c for c in base.cues}
            changed = {}
            for edit in edits:
                cue = by_id.get(edit.cueId)
                if cue is None:
                    raise ValueError("unknown cue")
                if edit.end_ms > base.duration_ms:
                    raise ValueError("cue ends after video")
                text = edit.text if edit.text.strip() else ""
                changed[cue.id] = cue.model_copy(
                    update={
                        "text_vi": text,
                        "start_ms": edit.start_ms,
                        "end_ms": edit.end_ms,
                        "status": "translated" if text else "unresolved",
                        "reason": None if text else "Blank caption saved by reviewer",
                    }
                )
            cues = tuple(changed.get(c.id, c) for c in base.cues)
            for cue in changed.values():
                for other in cues:
                    if (
                        other.id != cue.id
                        and cue.start_ms < other.end_ms
                        and cue.end_ms > other.start_ms
                    ):
                        raise ValueError("cue overlaps a neighbor")
            self.repo.adopt_marker(folder, base)
            before = self.repo.projection(folder)
            marker = safe_path(folder, ".projection.json")
            if marker.exists():
                local = json.loads(marker.read_bytes())
                dirty = local["fingerprint"] != self.repo.fingerprint(before)
                if dirty and local["revision"] != base.revision_id:
                    raise Conflict(base.revision_id, "Studio edits belong to an older revision")
            else:
                dirty = bool(before)
            if dirty:
                local_html = before.get("index.html", self.repo.read_asset(base.html))
                try:
                    require_visual_only(local_html, self.repo.read_asset(base.html), base.cues)
                except ValueError as exc:
                    raise Conflict(base.revision_id, str(exc)) from None
                visual = capture_visual(local_html, base.cues)
                extras = {
                    name: self.repo.asset(base, data, safe_path(folder, name).suffix)
                    for name, data in before.items()
                    if name != "index.html"
                }
            else:
                visual = capture_visual(self.repo.read_asset(base.html), base.cues)
                extras = {n: a for n, a in base.studio_files.items() if not n.startswith(RENDERS)}
            candidate = self.repo.prepare(base, cues, visual, studio_files=extras)
            if before != self.repo.projection(folder):
                raise Conflict(base.revision_id, "Studio changed during save; retry merge")
            self.repo.activate(candidate, etag)
            atomic_write(
                safe_path(folder, ".projection.json"),
                json.dumps(
                    {
                        "revision": base.revision_id,
                        "fingerprint": self.repo.fingerprint(before),
                    }
                ).encode(),
            )
        self.repo.materialize(aweme, generation)
        return self.project(aweme, generation)

    def reserve(self, aweme, generation, command, key):
        with self.repo.lock(aweme, generation) as folder:
            _, base, etag = self.repo.current(aweme, generation)
            if base.revision_id != command.baseRevision:
                raise Conflict(base.revision_id)
            cue = next((c for c in base.cues if c.id == command.cueId), None)
            if cue is None:
                raise ValueError("unknown cue")
            # A cue saved blank that still has a clip is asked to drop its voice from the mix.
            if not cue.text_vi.strip():
                if cue.clip is None:
                    raise ValueError("this cue has no voice to remove")
            elif cue.end_ms <= cue.start_ms:
                raise ValueError("save a nonblank caption and positive slot before revoice")
            self.repo.adopt_marker(folder, base)
            marker = safe_path(folder, ".projection.json")
            if marker.exists() and json.loads(marker.read_bytes())["fingerprint"] != (
                self.repo.fingerprint(self.repo.projection(folder))
            ):
                raise Conflict(base.revision_id, "Save Studio visual edits before revoice")
            record = {
                "command": command.model_dump(),
                "aweme_id": aweme,
                "generation": generation,
                "etag": etag,
                "hash": request_hash(base, cue),
            }
            try:
                self.repo.store.put_immutable_bytes(
                    key, json.dumps(record, sort_keys=True).encode()
                )
            except StorageConflictError:
                if self.repo.read_json(key)["command"] != command.model_dump():
                    raise Conflict(base.revision_id, "requestId collision") from None

    async def revoice(self, aweme, generation, command: RevoiceCue):
        async with self._dispatch_lock:
            key = self.repo.request_key(aweme, generation, command.requestId)
            known = await asyncio.to_thread(self.repo.optional_json, key)
            if known is not None:
                if known["command"] != command.model_dump():
                    raise Conflict(
                        self.repo.current(aweme, generation)[0].revision_id,
                        "requestId already belongs to another command",
                    )
            else:
                await asyncio.to_thread(self.reserve, aweme, generation, command, key)
            await asyncio.to_thread(self.claim_slot, key)
            try:
                await self.temporal.start_workflow(
                    RevoiceCaptionWorkflow.run,
                    key,
                    id=workflow_id(key),
                    task_queue="workflows",
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
            except WorkflowAlreadyStartedError:
                pass
            return {"requestId": command.requestId, "status": "accepted"}

    def reserve_cues(self, aweme, generation, command: RevoiceCues, key):
        """Records a batch revoice of `command.cueIds` on the revision they were saved in."""
        _, base, etag = self.repo.current(aweme, generation)
        if base.revision_id != command.baseRevision:
            raise Conflict(base.revision_id)
        by_id = {c.id: c for c in base.cues}
        record = {
            "command": command.model_dump(mode="json"),
            "aweme_id": aweme,
            "generation": generation,
            "etag": etag,
            "hash": batch_hash(base, [by_id[i] for i in command.cueIds]),
        }
        try:
            self.repo.store.put_immutable_bytes(key, json.dumps(record, sort_keys=True).encode())
        except StorageConflictError:
            if self.repo.read_json(key)["command"] != command.model_dump(mode="json"):
                raise Conflict(base.revision_id, "requestId collision") from None

    async def save_and_revoice(self, aweme, generation, command: SaveAndRevoice):
        """Saves the edited lines as one revision, then revoices those of them whose voice
        no longer matches, as one more: one mix, however many lines."""
        async with self._dispatch_lock:
            key = self.repo.request_key(aweme, generation, command.requestId)
            known = await asyncio.to_thread(self.repo.optional_json, key)
            if known is None:
                state = await asyncio.to_thread(
                    self.save_edits,
                    aweme,
                    generation,
                    command.expectedRevision,
                    command.headETag,
                    command.edits,
                )
                edited = {e.cueId for e in command.edits}
                ids = tuple(c["id"] for c in state["cues"] if c["id"] in edited and voiced(c))
                if not ids:
                    return {"status": "saved", "project": state, "cueIds": []}
                batch = RevoiceCues(
                    requestId=command.requestId, cueIds=ids, baseRevision=state["revision"]
                )
                await asyncio.to_thread(self.reserve_cues, aweme, generation, batch, key)
            else:
                if "cueIds" not in known["command"]:
                    raise Conflict(
                        self.repo.current(aweme, generation)[0].revision_id,
                        "requestId already belongs to another command",
                    )
                ids = tuple(known["command"]["cueIds"])
            await asyncio.to_thread(self.claim_slot, key)
            try:
                await self.temporal.start_workflow(
                    RevoiceBatchWorkflow.run,
                    args=[key, list(ids)],
                    id=workflow_id(key),
                    task_queue="workflows",
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
            except WorkflowAlreadyStartedError:
                pass
            return {
                "status": "accepted",
                "requestId": command.requestId,
                "cueIds": list(ids),
                "project": await asyncio.to_thread(self.project, aweme, generation),
            }

    def claim_slot(self, request_key):
        # Temporal visibility is eventually consistent, so it cannot enforce a hard
        # queue bound. CAS reservations also survive a lost start response/restart.
        queue_key = "review/bridge-queue.json"
        for _ in range(16):
            try:
                raw, etag = self.repo.store.read_version(queue_key)
                slots = json.loads(raw)
            except ArtifactMissingError:
                slots, etag = [], None
            if request_key in slots:
                return
            retained = [
                key
                for key in slots
                if self.repo.optional_json(key.removesuffix(".json") + ".result.json") is None
            ]
            if self.repo.optional_json(request_key.removesuffix(".json") + ".result.json"):
                return
            if len(retained) >= 16:
                raise QueueFull("Revoice queue is full; retry this request later")
            try:
                self.repo.store.compare_and_swap(
                    queue_key, json.dumps([*retained, request_key]).encode(), etag
                )
                return
            except StorageConflictError:
                continue
        raise QueueFull("Revoice queue changed; retry this request")

    async def job(self, aweme, generation, request_id):
        from temporalio.client import WorkflowExecutionStatus
        from temporalio.service import RPCError, RPCStatusCode

        key = self.repo.request_key(aweme, generation, request_id)
        if await asyncio.to_thread(self.repo.optional_json, key) is None:
            raise ValueError("unknown request")
        result = await asyncio.to_thread(
            self.repo.optional_json, key.removesuffix(".json") + ".result.json"
        )
        if result is not None:
            return result
        try:
            handle = self.temporal.get_workflow_handle(workflow_id(key))
            description = await handle.describe()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            return {"status": "reserved", "requestId": request_id}
        if description.status == WorkflowExecutionStatus.COMPLETED:
            return await handle.result()
        if description.status == WorkflowExecutionStatus.RUNNING:
            return {"status": "running", "requestId": request_id}
        return {"status": "failed", "requestId": request_id}


def workflow_id(key):
    return "revoice-" + hashlib.sha256(key.encode()).hexdigest()


def voiced(cue: dict) -> bool:
    """Whether a saved line's voice has to be made again: it no longer says the line or
    sits in its slot, and there is something to say (or a voice to drop)."""
    if not cue["mismatch"]:
        return False
    if cue["text_vi"].strip():
        return cue["end_ms"] > cue["start_ms"]
    return cue["clip"] is not None


def batch_hash(base, cues):
    return hashlib.sha256(
        json.dumps(
            {
                "cues": [c.model_dump(mode="json") for c in cues],
                "voice": base.voice.model_dump(mode="json"),
                "model": base.model,
                "engine": base.engine,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def request_hash(base, cue):
    return hashlib.sha256(
        json.dumps(
            {
                "cue": cue.model_dump(mode="json"),
                "voice": base.voice.model_dump(mode="json"),
                "model": base.model,
                "engine": base.engine,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
