"""Which TTS provider speaks a voice, and the Vbee voice a run names only by a marker.

A run carries its voice in its input, so in its workflow history. The Vbee voice is a
cloned voice of the account and its id is kept out of history like a credential: the
run names it with `VBEE_CONFIGURED_VOICE`, and the activities that speak or hash the
voice put the configured `VBEE_VOICE_ID` in its place (`ConfiguredVoices.resolve`).
Every cache key built from the resolved voice changes when the configured voice
changes, and only its hash ever leaves the activity.

`TtsByProvider` sends a voice to the provider it belongs to: a Vbee voice to Vbee, any
other to the provider the worker was always configured with. The queue, the synthesizer,
its clip cache and its heartbeats are the same for both.
"""

from pydantic import SecretStr

from localization.contracts.vbee_flow import VBEE_CONFIGURED_VOICE
from localization.contracts.voice import VoiceRef
from localization.providers.base import TtsProvider
from localization.providers.errors import PermanentProviderError

VBEE = "vbee"


class ConfiguredVoices:
    def __init__(self, vbee_voice_id: SecretStr | None) -> None:
        self._vbee = vbee_voice_id

    def resolve(self, voice: VoiceRef) -> VoiceRef:
        """The voice to speak and hash with. A Vbee voice must be the marker (an id that
        reached a run's input would already be in its history) and becomes the configured
        id; any other voice is returned as it is. Neither error names the id it saw."""
        if voice.provider != VBEE:
            return voice
        if voice.voice_id != VBEE_CONFIGURED_VOICE:
            raise PermanentProviderError(
                "vbee voice: a run must name the configured voice, not an id of its own"
            )
        configured = self._vbee.get_secret_value() if self._vbee is not None else ""
        if not configured:
            raise PermanentProviderError("vbee voice: VBEE_VOICE_ID is not set on this worker")
        # No validation: a rejected value would be echoed in the validation error.
        return voice.model_copy(update={"voice_id": configured})


class TtsByProvider:
    """A `TtsProvider` that hands each voice to the provider it belongs to."""

    def __init__(self, default: TtsProvider | None, vbee: TtsProvider | None = None) -> None:
        self._default = default
        self._vbee = vbee

    async def synthesize(self, text: str, voice: VoiceRef) -> bytes:
        provider = self._vbee if voice.provider == VBEE else self._default
        if provider is None:
            raise PermanentProviderError(
                f"tts: no provider for {voice.provider} voices is configured on this worker"
            )
        return await provider.synthesize(text, voice)
