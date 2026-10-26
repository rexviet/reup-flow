"""Settings, read from the environment. Credentials are `SecretStr`, so a repr, a
log line or a validation error never prints them."""

import ipaddress
import json
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from localization.contracts.voice import SETTINGS_BY_PROVIDER, Provider, VoiceRef

BBoxFormat = Literal["xyxy_px", "xyxy_norm1", "xyxy_norm1000", "yxyx_norm1000"]
Concurrency = Annotated[int, Field(ge=1, le=64)]


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

    voicestudio_url: str | None = None
    voicestudio_allow_private_http_cidrs: str = "100.64.0.0/10"
    voicestudio_api_key: SecretStr | None = None
    voice_provider: Provider = "voicestudio"
    voice_id: str | None = None
    voice_settings_json: str = "{}"
    target_lang: str = "vi"
    stt_model: str | None = None
    tts_model: str | None = None

    loc_database_url: SecretStr | None = None

    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key: SecretStr | None = None
    localizer_model: str | None = None
    dubber_model: str | None = None
    reviewer_model: str | None = None
    judge_model: str | None = None
    ocr_model: str | None = None
    ocr_bbox_format: BBoxFormat | None = None

    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str = "reup-media"
    s3_force_path_style: bool = True
    loc_s3_access_key_id: SecretStr | None = None
    loc_s3_secret_access_key: SecretStr | None = None

    stt_concurrency: Concurrency = 1
    tts_concurrency: Concurrency = 2
    media_concurrency: Concurrency = 2
    render_concurrency: Concurrency = 1
    llm_concurrency: Concurrency = 8
    io_concurrency: Concurrency = 2
    max_open_workflows: Concurrency = 10
    outbox_max_attempts: Concurrency = 5

    langfuse_host: str | None = None
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None

    demucs_bin: str = "demucs"
    provider_timeout_seconds: float = 120.0

    @field_validator("ocr_bbox_format", mode="before")
    @classmethod
    def _empty_format_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("voicestudio_url", "langfuse_host", mode="before")
    @classmethod
    def _empty_url_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("voicestudio_allow_private_http_cidrs")
    @classmethod
    def _cidrs_parse(cls, value: str) -> str:
        _parse_cidrs(value)
        return value

    def model_post_init(self, __context: object) -> None:
        cidrs = self.voicestudio_allow_private_http_cidrs
        if self.voicestudio_url is not None:
            check_endpoint_url("VOICESTUDIO_URL", self.voicestudio_url, cidrs)
        check_endpoint_url("OPENROUTER_BASE_URL", self.openrouter_base_url, cidrs)


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
