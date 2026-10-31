"""Whether a prepared video is a product advertisement, and so not worth localizing.

One call to Jev on the text of the transcript, nothing else: no frame, no audio. A video
without speech is not classified (there is nothing to judge it on) and goes on to be
localized, and so does every video while the check is switched off.

The activity answers True to skip the video. A failure is the provider's, classified like the
review's (permanent or transient); the workflow lets a video through when no answer could be
had, so a Jev outage never costs a localization.
"""

import asyncio
import logging
from collections.abc import Callable

from temporalio import activity

from localization.activities.localizer_ports import read_transcript_document
from localization.agents import ad_classifier
from localization.contracts import LocalizationInput, Transcript
from localization.observability.llm_usage import LlmUsage, Recorder, current_run
from localization.observability.tracing import tag_session
from localization.providers.jev import Jev
from localization.storage.object_store import ObjectStore

log = logging.getLogger(__name__)


class ClassifyVideoActivity:
    def __init__(
        self,
        store: ObjectStore,
        jev: Callable[[], Jev],
        *,
        threshold: float,
        enabled: bool = True,
        recorder: Recorder | None = None,
    ) -> None:
        self._store = store
        self._jev = jev
        self._threshold = threshold
        self._enabled = enabled
        self._recorder = recorder

    @activity.defn(name="classify_video")
    async def classify_video(self, spec: LocalizationInput, transcript: Transcript) -> bool:
        """True when the transcript promotes a product: the video is not localized."""
        tag_session(spec.aweme_id)
        if not self._enabled or transcript.segment_count == 0:
            return False
        doc = await asyncio.to_thread(
            read_transcript_document, self._store, transcript.transcript_key
        )
        state = ad_classifier.build_state(doc.segments)
        if state is None:
            return False
        result = await self._jev().ask(state, ad_classifier.QUESTIONS, self._record)
        skip = ad_classifier.is_advertisement(result.answers, self._threshold)
        log.info(
            "ad check: %s %.2f (threshold %.2f), %d ms, %d+%d tokens",
            "skip" if skip else "localize",
            result.answers[ad_classifier.QUESTION],
            self._threshold,
            result.latency_ms,
            result.input_tokens,
            result.output_tokens,
        )
        return skip

    async def _record(self, usage: LlmUsage) -> None:
        # Recorded under this activity's name, the stage `llm_calls` keeps it by.
        run = current_run()
        if self._recorder is not None and run is not None:
            await self._recorder(usage, run)
