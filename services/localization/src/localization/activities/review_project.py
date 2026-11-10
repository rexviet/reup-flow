"""Commit a closed, immutable review revision and resolve it only through durable HEAD."""

import asyncio
import base64
import hashlib
import html
import json
import tempfile
import uuid
from pathlib import Path

from temporalio import activity

from localization.activities.layout_captions import (
    FontMeter,
    caption_spans,
    check_one_translation,
    fit_at_size,
)
from localization.activities.script_document import read_script
from localization.activities.srt_document import srt_document
from localization.contracts import (
    DubbedAudio,
    DubSegmentsDocument,
    IngestResult,
    LocalizationInput,
    LocalizedScriptRef,
    ProjectRef,
    ReviewAsset,
    ReviewCue,
    ReviewHead,
    ReviewProjectManifest,
    SourceSnapshot,
    SubtitleDocument,
    SubtitleTrack,
)
from localization.storage.object_store import (
    ArtifactMissingError,
    ObjectStore,
    StorageConflictError,
    project_head_key,
    require_work_key,
)

# Exact preview/audit script bytes for the hyperframes@0.8.77 pin,
# verified in spikes/hyperframes-review/runtime-hashes.json. Pin changes require revalidation.
STUDIO_SCRIPT_HASHES = (
    "sha256-zcLdksF+4QBI6fkxZlhQ1MYhJBdseOJQ0RvIBMFDeh4=",
    "sha256-Ev6N67HGBwff9zraXAs7JeL3vnlbes4/itGiifbJZcg=",
    "sha256-xKHOrX/qi+sdXQhEmwncwWmJE9QoFBcSNaP6wHm/pWQ=",
    "sha256-wtEn1rpV5DbEVJ/gCdI8hx5LwJqDV8xii8bCgjbnHO8=",
    "sha256-m/Ec/hWgpgEFw7vdX1OqVjN4hYw91Db05P5gkOtgNdw=",
)


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def project_html(cues, video, mixed, font, subtitles, ingest, *, font_path) -> bytes:
    """Escaped text and fixed markup; no caption/reason becomes executable script."""
    duration = ingest.duration_ms / 1000
    video_name, mix_name, font_name = (Path(a.key).name for a in (video, mixed, font))
    # The caption and source mask share one vertical box. Measure the actual bundled
    # font, including padding/outline, instead of letting the browser wrap beyond it.
    top = subtitles.region.y if subtitles.region is not None else round(ingest.height * 0.8)
    height = subtitles.region.h if subtitles.region is not None else round(ingest.height * 0.14)
    meter = FontMeter(str(font_path))
    bottom = min(ingest.height, top + height)

    def box_style(width, box_height):
        percent = width / ingest.width * 100
        return (
            f"left:{(100 - percent) / 2:g}%;width:{percent:g}%;"
            f"top:{max(0, bottom - box_height)}px;height:{box_height}px"
        )

    default_box = box_style(ingest.width * 0.6, height)
    fitted, boxes = {}, {}
    visible = sorted((c for c in cues if c.text_vi.strip()), key=lambda c: c.start_ms)
    for c in visible:
        longest = max(meter.width(word, 20) for word in c.text_vi.split())
        width = max(ingest.width * 0.6, min(ingest.width * 0.9, longest + 16))
        while True:
            fit = fit_at_size(c.text_vi, meter, width - 16)
            if len(fit.lines) <= 2 or width >= ingest.width * 0.9:
                break
            width = min(ingest.width * 0.9, width + 16)
        box_height = max(height, len(fit.lines) * 28 + 8)
        if box_height > ingest.height:
            raise ValueError("caption box exceeds the frame at fixed 20px")
        fitted[c.id], boxes[c.id] = fit, box_style(width, box_height)
    geometry = "".join(f"#cue-{identity}{{{box}}}" for identity, box in boxes.items())
    style = f"""
@font-face{{font-family:Review;src:url('{font_name}')}}
body{{margin:0;background:#181818}}#review{{position:relative;overflow:hidden;
width:{ingest.width}px;height:{ingest.height}px}}video{{width:100%;height:100%}}
.caption,.source-mask{{position:absolute;left:20%;width:60%;
box-sizing:border-box}}
.caption{{top:{top}px;height:{height}px;padding:4px 8px;
display:flex;align-items:center;justify-content:center;font:20px/28px Review,sans-serif;
color:#ffff00;text-align:center;white-space:pre;-webkit-text-stroke:4px #000;
paint-order:stroke fill}}
.source-mask{{background:rgba(255,255,255,.25);backdrop-filter:blur(12px)}}
{geometry}
"""
    # Contiguous spans keep the box visible through gaps. Only a long caption's
    # own span expands; a long line elsewhere must not widen every other caption.
    spans, cursor = [], 0
    for c in visible:
        if c.start_ms > cursor:
            spans.append((cursor, c.start_ms, default_box))
        if c.end_ms > c.start_ms:
            spans.append((c.start_ms, c.end_ms, boxes[c.id]))
        cursor = c.end_ms
    if cursor < ingest.duration_ms:
        spans.append((cursor, ingest.duration_ms, default_box))
    merged = []
    for start, end, box in spans:
        if merged and merged[-1][1] == start and merged[-1][2] == box:
            merged[-1] = (merged[-1][0], end, box)
        else:
            merged.append((start, end, box))
    masks = [
        f'<div data-hf-id="hf-mask-{i}" id="mask-{i}" class="clip source-mask" '
        f'data-start="{start / 1000}" data-duration="{(end - start) / 1000}" data-track-index="1" '
        f'style="{box}"></div>'
        for i, (start, end, box) in enumerate(merged)
    ]
    captions = [
        f'<div data-hf-id="hf-cue-{c.id}" id="cue-{c.id}" class="clip caption" '
        f'data-start="{c.start_ms / 1000}" '
        f'data-duration="{(c.end_ms - c.start_ms) / 1000}" data-track-index="2">'
        f"{html.escape(chr(10).join(fitted[c.id].lines))}</div>"
        for c in cues
        if c.text_vi.strip()
    ]
    # The controls use only the mixed track. Cue clips are dependencies for rebuilding,
    # never extra audio elements. Studio owns playback inside its preview iframe.
    script = """const video=document.querySelector('video'),mix=document.querySelector('audio');
video.muted=true;
video.addEventListener('volumechange',()=>{if(!video.muted)video.muted=true;});
if(window.parent===window){
function sync(){mix.currentTime=video.currentTime;}
video.addEventListener('play',()=>{sync();mix.play().catch(()=>{});});
video.addEventListener('pause',()=>mix.pause());video.addEventListener('seeked',sync);
video.addEventListener('ratechange',()=>{mix.playbackRate=video.playbackRate;});
function tick(){for(const el of document.querySelectorAll('.clip')){
const start=Number(el.dataset.start),end=start+Number(el.dataset.duration);
el.style.visibility=video.currentTime>=start&&video.currentTime<end?'visible':'hidden';}
requestAnimationFrame(tick);}tick();}"""
    script_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    studio_scripts = " ".join(f"'{digest}'" for digest in STUDIO_SCRIPT_HASHES)
    data = json.dumps([c.model_dump(mode="json") for c in cues], ensure_ascii=False)
    data = data.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return f'''<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'self';
script-src 'sha256-{script_hash}' {studio_scripts}
http://127.0.0.1:3002/api/runtime.js; style-src 'unsafe-inline';
font-src 'self'; media-src 'self';
object-src 'none'; base-uri 'none'">
<title>Review project</title><style>{style}</style></head><body>
<main data-hf-id="hf-review" id="review" data-composition-id="review" data-start="0"
data-duration="{duration}" data-width="{ingest.width}" data-height="{ingest.height}"
data-no-timeline>
<video data-hf-id="hf-source" id="source" src="{video_name}"
data-start="0" data-duration="{duration}"
data-track-index="0" muted playsinline controls></video>
<audio data-hf-id="hf-mix" id="mix" src="{mix_name}" data-start="0" data-duration="{duration}"
data-track-index="3"></audio>
{"".join(masks)}{"".join(captions)}</main>
<script type="application/json" id="cue-data">{data}</script><script>{script}</script>
</body></html>'''.encode()


class ReviewProjectActivity:
    def __init__(self, store: ObjectStore, *, font_path: str, model: str) -> None:
        self._store = store
        self._font_path = font_path
        self._model = model

    @activity.defn(name="capture_review_source")
    async def capture_review_source(self, spec: LocalizationInput) -> SourceSnapshot:
        key = f"work/{spec.aweme_id}/g{spec.generation}/r0/source.mp4"
        return await asyncio.to_thread(self._store.capture_source, spec.video_key, key)

    @activity.defn(name="build_review_project")
    async def build_review_project(
        self,
        spec: LocalizationInput,
        dub: DubbedAudio,
        ingest: IngestResult,
        subtitles: SubtitleTrack,
        engine: str | None,
    ) -> ProjectRef:
        return await asyncio.to_thread(self._build, spec, dub, ingest, subtitles, engine)

    def _build(self, spec, dub, ingest, subtitles, engine) -> ProjectRef:
        source_snapshot = spec.source_snapshot
        if source_snapshot is None or spec.voice.provider != "voicestudio":
            raise ValueError("review projects require a captured VoiceStudio run")
        head_key = project_head_key(spec.aweme_id, spec.generation)
        try:
            resolve_project(self._store, head_key)
        except ArtifactMissingError as exc:
            if exc.key != head_key:
                raise
        else:
            return ProjectRef(project_key=head_key)
        for key in (
            dub.script_key,
            dub.segments_key,
            dub.audio_key,
            ingest.bgm_key,
            subtitles.subtitle_key,
            source_snapshot.snapshot_key,
        ):
            require_work_key(spec, key)
        script = read_script(self._store, dub.script_key, spec)
        doc = DubSegmentsDocument.model_validate_json(
            self._store.get_bytes(dub.segments_key, produced_by="dub_script")
        )
        check_one_translation(
            script,
            LocalizedScriptRef(script_key=dub.script_key, version=dub.script_version),
            dub,
            doc,
        )
        source_subs = SubtitleDocument.model_validate_json(
            self._store.get_bytes(subtitles.subtitle_key, produced_by="detect_subtitles")
        )
        if (
            len({s.id for s in doc.segments}) != len(doc.segments)
            or doc.aweme_id != spec.aweme_id
            or {s.id for s in doc.segments} != {s.id for s in script.segments}
        ):
            raise ValueError("dub identity/segment mismatch")
        prefix = f"review/{spec.aweme_id}/g{spec.generation}/assets/"
        with tempfile.TemporaryDirectory(prefix="review-project-") as tmp:
            root = Path(tmp)

            def asset_file(path, suffix):
                with path.open("rb") as src:
                    digest = hashlib.file_digest(src, "sha256").hexdigest()
                key = prefix + digest + suffix
                self._store.put_immutable_file(key, path)
                return ReviewAsset(key=key, sha256=digest)

            def asset_bytes(data, suffix):
                path = root / (checksum(data) + suffix)
                path.write_bytes(data)
                return asset_file(path, suffix)

            def copy_asset(key, suffix, producer):
                path = root / (uuid.uuid4().hex + suffix)
                self._store.download_file(key, path, produced_by=producer)
                return asset_file(path, suffix)

            video = copy_asset(source_snapshot.snapshot_key, ".mp4", "capture_review_source")
            if video.sha256 != source_snapshot.sha256:
                raise ValueError("source snapshot checksum mismatch")
            mixed = copy_asset(dub.audio_key, ".wav", "assemble_audio")
            if mixed.sha256 != dub.audio_sha256:
                raise ValueError("mixed audio checksum mismatch")
            try:
                bgm = copy_asset(ingest.bgm_key, ".wav", "ingest")
            except ArtifactMissingError:
                if any(s.status != "unresolved" for s in script.segments):
                    raise
                # The assembler already emitted BGM or silence with no spoken clips.
                bgm = mixed
            font = asset_file(Path(self._font_path), ".ttf")
            by_id = {s.id: s for s in doc.segments}
            spans = {
                i: (start, end)
                for i, start, end, _ in caption_spans(script, doc, ingest.duration_ms)
            }
            cues, copied_segments = [], []
            for seg in script.segments:
                audio = by_id[seg.id]
                if (audio.text_vi, audio.status, audio.reason) != (
                    seg.text_vi,
                    seg.status,
                    seg.reason,
                ):
                    raise ValueError("caption and active dub differ at initial commit")
                clip = (
                    copy_asset(audio.tts_key, ".wav", "synthesize")
                    if audio.tts_key is not None
                    else None
                )
                start, end = spans.get(seg.id, (seg.start_ms, seg.end_ms))
                cues.append(
                    ReviewCue(
                        id=seg.id,
                        source_segment_ids=(seg.id,),
                        start_ms=start,
                        end_ms=end,
                        text_vi=seg.text_vi,
                        status=seg.status,
                        reason=seg.reason,
                        clip=clip,
                        spoken_text=audio.text_vi,
                        audio_start_ms=seg.start_ms,
                        audio_duration_ms=audio.duration_ms,
                        stretch_ratio=audio.stretch_ratio,
                        overflow_unresolved=audio.overflow_unresolved,
                    )
                )
                copied_segments.append(
                    audio.model_copy(update={"tts_key": clip.key if clip else None})
                )
            revision = uuid.uuid4().hex
            manifest = ReviewProjectManifest(
                aweme_id=spec.aweme_id,
                generation=spec.generation,
                revision_id=revision,
                source=source_snapshot,
                script_version=script.version,
                voice=spec.voice,
                model=self._model,
                engine=engine,
                duration_ms=ingest.duration_ms,
                width=ingest.width,
                height=ingest.height,
                fps=ingest.fps,
                video=video,
                mixed_audio=mixed,
                bgm=bgm,
                font=font,
                script=asset_bytes(script.model_dump_json().encode(), ".json"),
                dub=asset_bytes(
                    doc.model_copy(update={"segments": tuple(copied_segments)})
                    .model_dump_json()
                    .encode(),
                    ".json",
                ),
                subtitles=asset_bytes(source_subs.model_dump_json().encode(), ".json"),
                html=asset_bytes(
                    project_html(
                        cues, video, mixed, font, source_subs, ingest, font_path=self._font_path
                    ),
                    ".html",
                ),
                srt=asset_bytes(srt_document(cues), ".srt"),
                cues=tuple(cues),
                source_region=source_subs.region,
                source_cues=tuple((c.start_ms, c.end_ms) for c in source_subs.cues),
            )
            data = manifest.model_dump_json().encode()
            key = f"review/{spec.aweme_id}/g{spec.generation}/r{revision}/manifest.json"
            self._store.put_immutable_bytes(key, data)
            head = ReviewHead(
                aweme_id=spec.aweme_id,
                generation=spec.generation,
                revision_id=revision,
                manifest_key=key,
                manifest_sha256=checksum(data),
                source=source_snapshot,
            )
            try:
                self._store.compare_and_swap(head_key, head.model_dump_json().encode(), None)
            except StorageConflictError:
                # Another initial writer (or a person's revision) already won. Never reset it.
                resolve_project(self._store, head_key)
        return ProjectRef(project_key=head_key)


def resolve_project(store: ObjectStore, head_key: str):
    data, etag = store.read_version(head_key)
    head = ReviewHead.model_validate_json(data)
    if head_key != project_head_key(head.aweme_id, head.generation):
        raise ValueError("HEAD path differs from identity")
    raw = store.get_bytes(head.manifest_key, produced_by="review_project")
    if checksum(raw) != head.manifest_sha256:
        raise ValueError("manifest checksum mismatch")
    manifest = ReviewProjectManifest.model_validate_json(raw)
    if (manifest.aweme_id, manifest.generation, manifest.revision_id, manifest.source) != (
        head.aweme_id,
        head.generation,
        head.revision_id,
        head.source,
    ):
        raise ValueError("HEAD and manifest differ")
    return head, manifest, etag


def materialize_project(store: ObjectStore, head_key: str, folder: Path):
    """Cold-open from HEAD; gather every dependency, verifying its bytes."""
    head, manifest, etag = resolve_project(store, head_key)
    folder.mkdir(parents=True, exist_ok=True)
    assets = (
        [
            getattr(manifest, name)
            for name in (
                "video",
                "mixed_audio",
                "bgm",
                "font",
                "script",
                "dub",
                "subtitles",
                "html",
                "srt",
            )
        ]
        + [c.clip for c in manifest.cues if c.clip is not None]
        + list(manifest.studio_files.values())
    )
    for asset in assets:
        path = folder / Path(asset.key).name
        store.download_file(asset.key, path, produced_by="review_project")
        with path.open("rb") as src:
            if hashlib.file_digest(src, "sha256").hexdigest() != asset.sha256:
                raise ValueError("asset checksum mismatch")
    for name, asset in manifest.studio_files.items():
        path = folder / name
        if not path.resolve().is_relative_to(folder.resolve()) or any(
            parent.is_symlink() for parent in [path, *path.parents]
        ):
            raise ValueError("unsafe Studio projection path")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((folder / Path(asset.key).name).read_bytes())
    (folder / "index.html").write_bytes((folder / Path(manifest.html.key).name).read_bytes())
    (folder / "captions.srt").write_bytes((folder / Path(manifest.srt.key).name).read_bytes())
    (folder / "manifest.json").write_text(manifest.model_dump_json())
    (folder / "head.json").write_text(head.model_dump_json())
    return manifest, etag
