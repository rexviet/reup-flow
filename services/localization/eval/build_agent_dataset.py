"""Extract real Dubber/Reviewer inputs from completed production histories and artifacts.

Published output is a reference, not a human gold label. Reviewer accuracy is left
unscored until expectedOutput is annotated with `issues` and `passed`.
Use --db-host postgres:5432 when running inside the compose network.
"""

import argparse
import html
import json
import os
import re
import tempfile
from pathlib import Path

from build_dataset import newest_runs
from common import LANGFUSE, TEMPORAL_UI, History, Langfuse, host_database_url, load_env

from localization.activities.assemble_audio import measure_loudness
from localization.activities.detect_subtitles import subtitles_key
from localization.config import Settings
from localization.contracts import DubSegmentsDocument, EditSpec, LocalizedScript
from localization.storage.object_store import ObjectStore


def dubber_input(history: History) -> dict | None:
    for activity in ("dubber_setup", "dubber_context"):
        for args, result in history.activities(activity):
            deps = args[0]
            context = result.get("context") if activity == "dubber_setup" and result else result
            if context:
                return dict(
                    context=context,
                    syllables_per_sec=deps["syllables_per_sec"],
                    feedback=[],
                    voice=deps["voice"],
                )
            # Code/tools get_segment returns the actual measured context as text.
            lines = {}
            tool_names = set()
            for event in history.events:
                scheduled = event.get("activityTaskScheduledEventAttributes")
                if not scheduled:
                    continue
                name = scheduled["activityType"]["name"]
                if "call_tool" not in name or "dubber" not in name or name in tool_names:
                    continue
                tool_names.add(name)
                for tool_args, text in history.activities(name):
                    call = tool_args[0]
                    if isinstance(text, dict):
                        text = text.get("result")
                    if call.get("name") != "get_segment" or not isinstance(text, str):
                        continue
                    text = text.replace("\\n", "\n")
                    match = re.search(
                        r"\[(\d+)\] source: (.*?)\nvietnamese: (.*?)</untrusted_video_text>"
                        r"\s*slot (\d+) ms,.*?speaking takes (\d+) ms",
                        text,
                        re.S,
                    )
                    if match:
                        ident, zh, vi, slot, duration = match.groups()
                        lines.setdefault(
                            int(ident),
                            dict(
                                id=int(ident),
                                src_zh=html.unescape(zh),
                                text_vi=html.unescape(vi).strip(),
                                slot_ms=int(slot),
                                duration_ms=int(duration),
                            ),
                        )
            if deps["pending_ids"] and all(i in lines for i in deps["pending_ids"]):
                return dict(
                    context=dict(lines=[lines[i] for i in deps["pending_ids"]]),
                    syllables_per_sec=deps["syllables_per_sec"],
                    feedback=[],
                    voice=deps["voice"],
                )
    return None


def reviewer_input(history: History, store: ObjectStore) -> dict | None:
    calls = [call for call in history.activities("review_before_render") if call[1] is not None]
    if not calls:
        calls = history.activities("review_render")
    if not calls:
        return None
    args, _ = calls[0]
    # Render review has one extra RenderResult argument before the script reference.
    offset = 1 if "render_key" in args[1] else 0
    script, dub, spec = args[1 + offset : 4 + offset]

    def read(key: str) -> dict:
        return json.loads(store.get_bytes(key, produced_by="eval"))

    script_doc = LocalizedScript.model_validate(read(script["script_key"]))
    dub_doc = DubSegmentsDocument.model_validate(read(dub["segments_key"]))
    edit = EditSpec.model_validate(read(spec["spec_key"]))
    with tempfile.TemporaryDirectory(prefix="review-eval-") as root:
        audio = Path(root) / ("render.mp4" if offset else "dubbed.wav")
        key = args[1]["render_key"] if offset else dub["audio_key"]
        store.download_file(key, audio, produced_by="eval")
        loudness = measure_loudness(audio, -14, -1.5)
    return dict(
        script=script_doc.model_dump(mode="json"),
        dub=dub_doc.model_dump(mode="json"),
        spec=edit.model_dump(mode="json"),
        loudness=dict(lufs=float(loudness["input_i"]), true_peak_dbtp=float(loudness["input_tp"])),
        subtitles=read(subtitles_key(script_doc.aweme_id)) if edit.cover else None,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=("dubber", "reviewer"), required=True)
    parser.add_argument("--videos", type=int, default=5)
    parser.add_argument("--dataset")
    parser.add_argument("--status", default="published,needs_human")
    parser.add_argument("--db-host", default="localhost:5432")
    parser.add_argument("--temporal-ui", default=TEMPORAL_UI)
    parser.add_argument("--langfuse", default=LANGFUSE)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.videos < 1:
        parser.error("--videos must be positive")
    load_env()
    lf = Langfuse.from_env(args.langfuse)
    store = ObjectStore.from_settings(Settings()) if args.agent == "reviewer" else None
    dataset = args.dataset or f"{args.agent}-v1"
    lf.ensure_dataset(dataset, f"Real {args.agent} inputs; human labels pending")
    cases = []
    database = host_database_url(os.environ["LOC_DATABASE_URL"], args.db_host)
    for aweme, generation, workflow_id in newest_runs(database, args.status.split(",")):
        history = History.fetch(args.temporal_ui, workflow_id)
        inp = dubber_input(history) if args.agent == "dubber" else reviewer_input(history, store)
        if not inp:
            continue
        metadata = dict(
            pool="real",
            workflow_id=workflow_id,
            generation=generation,
            label_source="pending_human",
            aweme_id=aweme,
        )
        expected = dict(required={}) if args.agent == "dubber" else {}
        ident = f"{dataset}-{aweme}"
        lf.upsert_item(dataset, ident, inp, expected, metadata)
        cases.append(dict(id=ident, input=inp, expectedOutput=expected, metadata=metadata))
        print(ident, flush=True)
        if len(cases) >= args.videos:
            break
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases))
    print(f"{len(cases)}/{args.videos} real items")
    return 0 if len(cases) == args.videos else 1


if __name__ == "__main__":
    raise SystemExit(main())
