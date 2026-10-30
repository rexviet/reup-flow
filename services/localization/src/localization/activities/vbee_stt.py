"""What is said in the separated vocals, heard by Vbee (which answers Chinese speech in
Vietnamese). The vocals are read from the store; only the transcript leaves."""

import asyncio
from typing import Protocol

from temporalio import activity

from localization.activities.errors import provider_errors
from localization.activities.synthesize import background_heartbeat
from localization.contracts import LocalizationInput
from localization.contracts.vbee_flow import SpeechTranscript
from localization.observability.tracing import tag_session
from localization.providers.errors import PermanentProviderError
from localization.storage.object_store import ObjectStore


class SpeechToText(Protocol):
    async def transcribe(self, wav: bytes) -> SpeechTranscript: ...


class VbeeSttActivity:
    def __init__(self, store: ObjectStore, stt: SpeechToText | None) -> None:
        self._store = store
        self._stt = stt

    @activity.defn(name="vbee_stt")
    async def vbee_stt(self, spec: LocalizationInput, vocals_key: str) -> SpeechTranscript:
        tag_session(spec.aweme_id)
        with provider_errors():
            if self._stt is None:
                raise PermanentProviderError(
                    "vbee stt: VBEE_APP_ID and VBEE_API_TOKEN are not set on this worker"
                )
            vocals = await asyncio.to_thread(
                self._store.get_bytes, vocals_key, produced_by="ingest"
            )
            # The job is polled for minutes; the heartbeat says the worker is alive.
            async with background_heartbeat():
                return await self._stt.transcribe(vocals)
