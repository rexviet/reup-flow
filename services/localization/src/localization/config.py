"""Settings, read from the environment. Credentials are `SecretStr`, so a repr, a
log line or a validation error never prints them."""

import ipaddress
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from localization.contracts.voice import VoiceRef, VoiceStudioSettings

BBoxFormat = Literal["xyxy_px", "xyxy_norm1", "xyxy_norm1000", "yxyx_norm1000"]


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
    voicestudio_allow_private_http_cidrs: str = ""
    voicestudio_api_key: SecretStr | None = None
    voice_id: str | None = None
    stt_model: str | None = None
    tts_model: str | None = None

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

    demucs_bin: str = "demucs"
    provider_timeout_seconds: float = 120.0

    @field_validator("ocr_bbox_format", mode="before")
    @classmethod
    def _empty_format_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("voicestudio_url", mode="before")
    @classmethod
    def _empty_url_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("voicestudio_allow_private_http_cidrs")
    @classmethod
    def _cidrs_parse(cls, value: str) -> str:
        _parse_cidrs(value)
        return value

    def model_post_init(self, __context: object) -> None:
        if self.voicestudio_url is not None:
            check_voicestudio_url(self.voicestudio_url, self.voicestudio_allow_private_http_cidrs)


def check_voicestudio_url(url: str, allow_private_http_cidrs: str) -> None:
    """https is always fine. Plain http is accepted only for an IP literal inside
    the operator's allow-list, because the bearer token and the audio cross the
    wire in the clear. Anything else refuses to start."""
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise ConfigError("VOICESTUDIO_URL must not embed credentials")
    host = parts.hostname
    if host is None or parts.scheme not in ("http", "https"):
        raise ConfigError("VOICESTUDIO_URL must be an http(s) URL with a host")
    if parts.scheme == "https":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ConfigError(
            "VOICESTUDIO_URL uses http with a hostname; use https or an allow-listed IP"
        ) from None
    if not any(address in net for net in _parse_cidrs(allow_private_http_cidrs)):
        raise ConfigError(
            "VOICESTUDIO_URL uses plain http outside VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS"
        )


def load_settings() -> Settings:
    return Settings()


def voice_ref_from_settings(settings: Settings) -> VoiceRef:
    """The voice every new workflow is started with. Carries the voice id only;
    the API key stays in the provider's settings."""
    if not settings.voice_id:
        raise ConfigError("VOICE_ID is not set")
    return VoiceRef(
        provider="voicestudio", voice_id=settings.voice_id, settings=VoiceStudioSettings()
    )
