"""Stand-ins for the layout, render, review and publish steps, registered under the
real activity names.

The layout and the render are fakes that behave like the real ones in the two ways the
workflow depends on: a spec's hash follows what the spec is made from, and a spec that
is rendered again does not render again. Publishing is the REAL activity over an object
store on a directory and a run table in memory, so what lands in `output/` and
`review/` is what the real code would put there.
"""

import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from temporalio import activity

from localization.activities.layout_captions import LayoutCaptionsActivity
from localization.activities.publish import PublishActivity
from localization.activities.review_render import ReviewRenderActivity
from localization.contracts import (
    DubbedAudio,
    EditSpecRef,
    IngestResult,
    LocalizationInput,
    LocalizedScriptRef,
    RenderInput,
    RenderResult,
    ReviewIssue,
    ReviewScores,
    ReviewVerdict,
    SegmentRoom,
    SegmentRooms,
    SubtitleTrack,
)
from localization.storage.object_store import ObjectStore, work_key
from tests.support.file_s3 import FileS3


def verdict(*issues: ReviewIssue, passed: bool | None = None) -> ReviewVerdict:
    good = (not issues) if passed is None else passed
    return ReviewVerdict(
        passed=good,
        scores=ReviewScores(fidelity=0.9, timing=0.9, visual=0.9),
        issues=tuple(issues),
    )


def issue(stage: str, reason: str = "something is wrong", segment_id: int | None = None):
    return ReviewIssue(stage=stage, reason=reason, segment_id=segment_id)  # type: ignore[arg-type]


class MemoryRuns:
    """The `localization.runs` table, in memory: one row per (video, generation)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, int], dict] = {}
        self.writes = 0

    async def upsert(self, *, aweme_id: str, generation: int, **row) -> None:
        self.writes += 1
        self.rows[(aweme_id, generation)] = row


@dataclass
class ReviewFakes:
    # What each successive review says; the last one is repeated.
    verdicts: list[ReviewVerdict] = field(default_factory=lambda: [verdict()])
    # Rounds whose layout differs from what the same inputs gave before (the layout is
    # deterministic, so by default a rerun of it changes nothing).
    changed_layout_in_rounds: set[int] = field(default_factory=set)
    # Segment id -> (ms to the end of the video, slot in ms) for the lines that are tight;
    # every other line of the script has all the time it could want.
    tight: dict[int, tuple[int, int]] = field(default_factory=dict)
    # (activity, nth call) -> an error that call raises instead of answering.
    fail_on: dict[tuple[str, int], Exception] = field(default_factory=dict)
    root: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="review-fakes-")))
    calls: Counter = field(default_factory=Counter)
    layouts: list[dict] = field(default_factory=list)
    renders: list[str] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)
    # How many times a spec was really rendered (a spec already rendered is not).
    rendered: Counter = field(default_factory=Counter)
    runs: MemoryRuns = field(default_factory=MemoryRuns)
    # What a render writes (a real video, for the tests that look at its frames).
    render_bytes: bytes | None = None
    # The real review activity, to run instead of the stand-in that answers from `verdicts`.
    real_review: ReviewRenderActivity | None = None
    # The reviewer looks at frames of the video (the `llm` mode): asked before the render,
    # it answers that it needs one, and is asked again with the render of the round.
    needs_render: bool = False
    # The real layout, to run instead of the stand-in that hashes what it is given.
    real_layout: LayoutCaptionsActivity | None = None

    def __post_init__(self) -> None:
        self.s3 = FileS3(self.root)
        self.store = ObjectStore(self.s3, "reup-media")
        self._spec_sha: dict[str, str] = {}

    def _enter(self, name: str) -> None:
        self.calls[name] += 1
        error = self.fail_on.get((name, self.calls[name]))
        if error is not None:
            raise error

    def activities(self) -> list[Callable]:
        fakes = self
        publisher = PublishActivity(self.store, self.runs)  # type: ignore[arg-type]

        @activity.defn(name="layout_captions")
        async def layout_captions(
            spec: LocalizationInput,
            script: LocalizedScriptRef,
            subtitles: SubtitleTrack,
            ingest: IngestResult,
            dub: DubbedAudio,
            round: int,
        ) -> EditSpecRef:
            fakes._enter("layout_captions")
            if fakes.real_layout is not None:
                made = await fakes.real_layout.layout_captions(
                    spec, script, subtitles, ingest, dub, round
                )
                fakes._spec_sha[made.spec_key] = made.spec_sha256
                fakes.layouts.append(
                    {"round": round, "script_key": script.script_key, "sha": made.spec_sha256}
                )
                return made
            # What the real layout puts in the spec: the script it reads and the track.
            variant = "x" if round in fakes.changed_layout_in_rounds else ""
            digest = sha256(
                f"{script.script_key}|{script.version}|{dub.audio_key}|{dub.audio_sha256}|"
                f"{dub.segments_key}|{variant}".encode()
            ).hexdigest()
            key = work_key(spec.aweme_id, round, "edit-spec.json")
            fakes._spec_sha[key] = digest
            fakes.layouts.append(
                {
                    "round": round,
                    "script_key": script.script_key,
                    "script_version": script.version,
                    "audio_key": dub.audio_key,
                    "segments_key": dub.segments_key,
                    "sha": digest,
                }
            )
            return EditSpecRef(
                spec_key=key,
                spec_sha256=digest,
                script_version=script.version,
                audio_sha256=dub.audio_sha256,
            )

        @activity.defn(name="segment_room")
        async def segment_room(
            spec: LocalizationInput, script: LocalizedScriptRef, video_duration_ms: int
        ) -> SegmentRooms:
            fakes._enter("segment_room")
            return SegmentRooms(
                items=tuple(
                    SegmentRoom(id=i, to_end_ms=to_end, slot_ms=slot)
                    for i in range(10)
                    for to_end, slot in [fakes.tight.get(i, (20_000, 3000))]
                )
            )

        @activity.defn(name="render")
        async def render(request: RenderInput) -> RenderResult:
            fakes._enter("render")
            sha = fakes._spec_sha[request.spec_key]
            key = f"work/{request.aweme_id}/render-{sha}.mp4"
            fakes.renders.append(key)
            if not fakes.store.exists(key):
                fakes.rendered[sha] += 1
                fakes.store.put_bytes(key, fakes.render_bytes or b"mp4:" + sha.encode())
            return RenderResult(render_key=key, spec_sha256=sha)

        def answer(
            round: int,
            render_key: str | None,
            script: LocalizedScriptRef,
            dub: DubbedAudio,
            spec_ref: EditSpecRef,
        ) -> ReviewVerdict:
            fakes.reviews.append(
                {
                    "round": round,
                    "render_key": render_key,
                    "script_key": script.script_key,
                    "audio_key": dub.audio_key,
                    "spec_sha": spec_ref.spec_sha256,
                }
            )
            index = min(len(fakes.reviews) - 1, len(fakes.verdicts) - 1)
            return fakes.verdicts[index]

        @activity.defn(name="review_before_render")
        async def review_before_render(
            spec: LocalizationInput,
            script: LocalizedScriptRef,
            dub: DubbedAudio,
            spec_ref: EditSpecRef,
            round: int,
        ) -> ReviewVerdict | None:
            fakes._enter("review_before_render")
            if fakes.real_review is not None:
                return await fakes.real_review.review_before_render(
                    spec, script, dub, spec_ref, round
                )
            if fakes.needs_render:
                return None
            return answer(round, None, script, dub, spec_ref)

        @activity.defn(name="review_render")
        async def review_render(
            spec: LocalizationInput,
            render: RenderResult,
            script: LocalizedScriptRef,
            dub: DubbedAudio,
            spec_ref: EditSpecRef,
            round: int,
        ) -> ReviewVerdict:
            fakes._enter("review_render")
            if fakes.real_review is not None:
                return await fakes.real_review.review_render(
                    spec, render, script, dub, spec_ref, round
                )
            return answer(round, render.render_key, script, dub, spec_ref)

        return [
            layout_captions,
            render,
            review_before_render,
            review_render,
            segment_room,
            publisher.publish,
        ]
