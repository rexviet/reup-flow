"""Settings, read from the environment. Credentials are `SecretStr`, so a repr, a
log line or a validation error never prints them."""

import ipaddress
import json
import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from localization.contracts.voice import SETTINGS_BY_PROVIDER, Provider, VoiceRef

BBoxFormat = Literal["xyxy_px", "xyxy_norm1", "xyxy_norm1000", "yxyx_norm1000"]
Concurrency = Annotated[int, Field(ge=1, le=64)]
# How an agent answers: `single` preloads everything it would look up into one prompt
# (one model request, at most one more to repair lines that break their checks);
# `tools` lets it call tools in a loop, one model request per call.
AgentMode = Literal["single", "tools"]
# The Dubber has a third: `code`, where it writes code that calls its tools (the same as
# `localization.agents.dubber.DubberMode`, kept here so settings import no agent).
DubberMode = Literal["single", "tools", "code"]
# How the Reviewer judges a render. `jev`: checks in code plus one call to Jev, a typed
# decision model, for what needs understanding. `llm`: the tool-calling vision agent.
ReviewerMode = Literal["jev", "llm"]
# Whether the one-request Localizer and Dubber ask the model for the answer only, within a
# token cap sized to the script (`off`), or send the plain request and let it reason
# (`default`).
ReasoningMode = Literal["off", "default"]
# Which workflow the starter starts for a video. `voicestudio`: VideoLocalizationWorkflow
# (VoiceStudio STT and TTS, the Localizer, the Dubber, the Jev review). `vbee`:
# VbeeDubbingWorkflow (Vbee STT that answers in Vietnamese, and Vbee's cloned voice).
Pipeline = Literal["voicestudio", "vbee"]


def default_caption_font(module_path: Path) -> Path:
    """The font next to a source checkout: <repo>/assets/fonts/..., four levels above this file.

    In a container the module sits at /app/src/localization/config.py, which has no fourth
    parent; the font is mounted at /assets and CAPTION_FONT_PATH says where, so the fallback
    is the filesystem root.
    """
    parents = module_path.resolve().parents
    root = parents[4] if len(parents) > 4 else Path(parents[-1])
    return root / "assets" / "fonts" / "BeVietnamPro-SemiBold.ttf"


# <repo>/services/localization/src/localization/config.py.
DEFAULT_CAPTION_FONT = default_caption_font(Path(__file__))
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


class ConfigError(ValueError):
    pass


def _parse_cidrs(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [
        ipaddress.ip_network(part.strip(), strict=False) for part in raw.split(",") if part.strip()
    ]


class Settings(BaseSettings):
    # `hide_input_in_errors`: a rejected VOICESTUDIO_URL may have been pasted with
    # credentials in it, and pydantic would otherwise echo the whole value.
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    localization_pipeline: Pipeline = "voicestudio"

    voicestudio_url: str | None = None
    voicestudio_allow_private_http_cidrs: str = "100.64.0.0/10"
    voicestudio_api_key: SecretStr | None = None
    voice_provider: Provider = "voicestudio"
    voice_id: str | None = None
    voice_settings_json: str = "{}"
    target_lang: str = "vi"
    stt_model: str | None = None
    # Off for a VoiceStudio ASR engine that has no `multilingual` mode (MLX Whisper): each
    # chunk is then detected on its own and decoded at temperature 0.
    stt_multilingual: bool = True
    tts_model: str | None = None

    loc_database_url: SecretStr | None = None

    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key: SecretStr | None = None
    localizer_model: str | None = None
    dubber_model: str | None = None
    localizer_mode: AgentMode = "single"
    dubber_mode: DubberMode = "single"
    localizer_reasoning: ReasoningMode = "off"
    dubber_reasoning: ReasoningMode = "off"
    reviewer_model: str | None = None
    localization_delivery_mode: Literal["legacy_render", "review_project"] = "legacy_render"
    reviewer_mode: ReviewerMode = "jev"
    # Jev is reached through OpenRouter's `/systemone`; the key and base URL are the
    # OpenRouter ones. Pinned, so a model that moves under an alias cannot change what
    # a review means.
    reviewer_jev_model: str = "typesafe/jev-1.13"
    # Once a video is prepared, Jev (the model above) is asked whether its speech promotes
    # a product; at or over the threshold the video is not localized and its run is left
    # as `skipped_ad`. Off, every video is localized.
    ad_gate_enabled: bool = True
    ad_gate_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    # Before anything else, the source's audio is checked for a voice: when less than this
    # share of it is speech, the video is not localized, its original is published as it is
    # and its run is left as `published_original`. Off, every video is localized.
    voice_check_enabled: bool = True
    voice_check_min_speech_share: float = Field(default=0.05, ge=0.0, le=1.0)
    judge_model: str | None = None
    ocr_model: str | None = None
    ocr_bbox_format: BBoxFormat | None = None
    # An audio-capable model (through OpenRouter) asked whether each transcript segment's
    # vocals are sung; a sung segment keeps its original sound and is not dubbed. Unset,
    # nothing is asked and every segment is dubbed as speech.
    singing_model: str | None = None

    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str = "reup-media"
    s3_force_path_style: bool = True
    loc_s3_access_key_id: SecretStr | None = None
    loc_s3_secret_access_key: SecretStr | None = None

    stt_concurrency: Concurrency = 1
    tts_concurrency: Concurrency = 2
    media_concurrency: Concurrency = 2
    # Separations at once on the host worker (one at a time: Demucs on the GPU already uses it
    # all).
    ingest_concurrency: Concurrency = 1
    render_concurrency: Concurrency = 1
    llm_concurrency: Concurrency = 8
    io_concurrency: Concurrency = 2
    voice_check_concurrency: Concurrency = 2
    max_open_workflows: Concurrency = 10
    outbox_max_attempts: Concurrency = 5

    langfuse_host: str | None = None
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    # Whether the agents' spans carry what they say and do: every prompt and answer, the
    # code the Dubber writes, each tool call's arguments and result. Off, a trace keeps the
    # shape of each run (which tool was called when), timings, token counts and costs, and
    # none of that text.
    trace_agent_content: bool = True

    # The one font file captions are measured with here and drawn with by the renderer.
    caption_font_path: str = str(DEFAULT_CAPTION_FONT)
    cover_color: str = "#101010"
    cover_opacity: float = Field(default=0.92, ge=0.0, le=1.0)

    loudness_lufs: float = Field(default=-14.0, le=0.0, ge=-40.0)
    # The original voice under the dubbed one, as a fraction of its own level (0.3 is 70%
    # quieter, about -10 dB); 0 leaves it out. Sung spans keep it at full level regardless.
    original_voice_gain: float = Field(default=0.25, ge=0.0, le=1.0)
    true_peak_dbtp: float = Field(default=-1.5, le=0.0, ge=-9.0)

    demucs_bin: str = "demucs"
    provider_timeout_seconds: float = 120.0
    # Local TTS includes waiting for the shared inference engine. Keep its deadline
    # separate from STT/OCR and below the synthesis activity's 30-minute budget.
    tts_timeout_seconds: float = Field(default=1200.0, gt=0, le=1500)
    # True when a separate `voice` worker (run on the host) serves the `stt` and `tts` queues:
    # the `agents` worker then leaves them alone, so no speech task is picked up by a process
    # that has to reach VoiceStudio through the container network.
    voice_queues_on_host: bool = False

    # Vbee speech (STT and batch TTS). The voice id names a cloned voice of the account
    # and is kept out of output like the credentials are.
    vbee_base_url: str = "https://api.vbee.vn"
    vbee_app_id: SecretStr | None = None
    vbee_api_token: SecretStr | None = None
    vbee_voice_id: SecretStr | None = None
    # ZeroTTS (a voice that runs on this machine): the folder of the model's real files, the
    # models held in memory (one per request spoken at once) and the CPU threads of each.
    zerotts_model_path: str | None = None
    zerotts_instances: Concurrency = 2
    zerotts_threads: Annotated[int, Field(ge=1, le=32)] = 2
    # The ZeroTTS voice new workflows are spoken with, by the id it is installed under on the
    # host (read by the starter). Empty keeps VOICE_ID, a voice of the VoiceStudio provider.
    zerotts_voice_id: str | None = None
    # How long one STT job or one TTS request is polled before it counts as lost.
    vbee_poll_timeout_seconds: float = Field(default=300.0, gt=0.0, le=3600.0)

    @field_validator("ocr_bbox_format", mode="before")
    @classmethod
    def _empty_format_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("voicestudio_url", "langfuse_host", mode="before")
    @classmethod
    def _empty_url_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("cover_color")
    @classmethod
    def _cover_color_is_hex(cls, value: str) -> str:
        if _COLOR_RE.fullmatch(value) is None:
            raise ValueError("COVER_COLOR must look like #101010")
        return value.lower()

    @field_validator("voicestudio_allow_private_http_cidrs")
    @classmethod
    def _cidrs_parse(cls, value: str) -> str:
        _parse_cidrs(value)
        return value

    @property
    def jev_base_url(self) -> str:
        """The SDK adds `/v1/systemone` itself, so it is given the OpenRouter base
        without its `/v1`."""
        return self.openrouter_base_url.rstrip("/").removesuffix("/v1")

    def model_post_init(self, __context: object) -> None:
        cidrs = self.voicestudio_allow_private_http_cidrs
        if self.voicestudio_url is not None:
            check_endpoint_url("VOICESTUDIO_URL", self.voicestudio_url, cidrs)
        check_endpoint_url("OPENROUTER_BASE_URL", self.openrouter_base_url, cidrs)
        check_endpoint_url("VBEE_BASE_URL", self.vbee_base_url, cidrs)


def check_endpoint_url(name: str, url: str, allow_private_http_cidrs: str) -> None:
    """https is always fine. Plain http is accepted only for an IP literal inside
    the operator's allow-list, because a bearer token and the payload would cross
    the wire in the clear. Anything else refuses to start."""
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise ConfigError(f"{name} must not embed credentials")
    host = parts.hostname
    if host is None or parts.scheme not in ("http", "https"):
        raise ConfigError(f"{name} must be an http(s) URL with a host")
    if parts.scheme == "https":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ConfigError(
            f"{name} uses http with a hostname; use https or an allow-listed IP"
        ) from None
    if not any(address in net for net in _parse_cidrs(allow_private_http_cidrs)):
        raise ConfigError(f"{name} uses plain http outside VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS")


def check_voicestudio_url(url: str, allow_private_http_cidrs: str) -> None:
    check_endpoint_url("VOICESTUDIO_URL", url, allow_private_http_cidrs)


def load_settings() -> Settings:
    return Settings()


def voice_ref_from_settings(settings: Settings) -> VoiceRef:
    """The voice every new workflow is started with. Carries the voice id and the
    provider's closed settings model only; the API key stays in the provider's
    settings."""
    if not settings.voice_id:
        raise ConfigError("VOICE_ID is not set")
    try:
        raw = json.loads(settings.voice_settings_json or "{}")
        if not isinstance(raw, dict):
            raise ValueError
        voice_settings = SETTINGS_BY_PROVIDER[settings.voice_provider].model_validate(raw)
        return VoiceRef(
            provider=settings.voice_provider,
            voice_id=settings.voice_id,
            settings=voice_settings,  # type: ignore[arg-type]
        )
    except (ValueError, ValidationError):
        # Never echo the value: a mistyped settings blob may hold a credential.
        raise ConfigError("VOICE_SETTINGS_JSON is not a valid settings object") from None
