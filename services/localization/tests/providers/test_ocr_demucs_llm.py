import json
import os
import stat
from pathlib import Path

import pytest
from tests.support.stub_server import Scenario, StubServer

from localization.config import ConfigError, Settings
from localization.providers.bbox import normalize_bbox
from localization.providers.demucs import DemucsSeparator
from localization.providers.errors import PermanentProviderError, TransientProviderError
from localization.providers.llm import model_for
from localization.providers.vision_ocr import VisionOcr


@pytest.mark.parametrize(
    "fmt, raw, expected",
    [
        ("xyxy_px", [100, 600, 900, 680], (100, 600, 800, 80)),
        ("xyxy_norm1", [0.1, 0.8, 0.9, 0.9], (192, 864, 1536, 108)),
        ("xyxy_norm1000", [100, 800, 900, 900], (192, 864, 1536, 108)),
        ("yxyx_norm1000", [800, 100, 900, 900], (192, 864, 1536, 108)),
    ],
)
def test_bbox_formats_normalise_to_pixel_xywh(fmt: str, raw: list[float], expected: tuple) -> None:
    box = normalize_bbox(raw, fmt, 1920, 1080)  # type: ignore[arg-type]
    assert (box.x, box.y, box.w, box.h) == expected


def test_bbox_is_clamped_to_the_frame_and_empty_boxes_are_refused() -> None:
    box = normalize_bbox([-50, 1000, 5000, 5000], "xyxy_px", 1920, 1080)
    assert (box.x, box.y, box.x + box.w, box.y + box.h) == (0, 1000, 1920, 1080)
    with pytest.raises(PermanentProviderError):
        normalize_bbox([10, 10, 10, 50], "xyxy_px", 1920, 1080)
    with pytest.raises(PermanentProviderError):
        normalize_bbox([5000, 5000, 6000, 6000], "xyxy_px", 1920, 1080)
    with pytest.raises(PermanentProviderError):
        normalize_bbox([1, 2, 3], "xyxy_px", 1920, 1080)  # type: ignore[arg-type]


def _ocr_settings(stub: StubServer, monkeypatch: pytest.MonkeyPatch, fmt: str | None) -> Settings:
    monkeypatch.setenv("OPENROUTER_BASE_URL", stub.url)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OCR_MODEL", "vision/model")
    if fmt:
        monkeypatch.setenv("OCR_BBOX_FORMAT", fmt)
    return Settings()


def _chat(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


async def test_vision_ocr_posts_the_frame_and_returns_a_pixel_box(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub.scenario = Scenario(json_body=_chat('```json\n{"bbox": [100, 800, 900, 900]}\n```'))
    ocr = VisionOcr.from_settings(_ocr_settings(stub, monkeypatch, "xyxy_norm1000"))
    box = await ocr.detect_region(b"\x89PNGdata", 1920, 1080)
    assert box is not None and (box.x, box.y, box.w, box.h) == (192, 864, 1536, 108)
    request = stub.requests[0]
    assert request.path == "/chat/completions"
    body = json.loads(request.body)
    assert body["model"] == "vision/model"
    assert body["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


async def test_vision_ocr_null_bbox_means_no_burned_in_subtitles(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub.scenario = Scenario(json_body=_chat('{"bbox": null}'))
    ocr = VisionOcr.from_settings(_ocr_settings(stub, monkeypatch, "xyxy_px"))
    assert await ocr.detect_region(b"x", 100, 100) is None


async def test_vision_ocr_unparseable_reply_is_permanent(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub.scenario = Scenario(json_body=_chat("the subtitles are at the bottom"))
    ocr = VisionOcr.from_settings(_ocr_settings(stub, monkeypatch, "xyxy_px"))
    with pytest.raises(PermanentProviderError):
        await ocr.detect_region(b"x", 100, 100)


def test_vision_ocr_refuses_to_build_without_a_bbox_format(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(PermanentProviderError, match="OCR_BBOX_FORMAT"):
        VisionOcr.from_settings(_ocr_settings(stub, monkeypatch, None))


# --- demucs -----------------------------------------------------------------

FAKE_DEMUCS = """#!/bin/sh
# argv: demucs --two-stems=vocals -o OUT -- INPUT
out="$3"
[ "$4" = "--" ] || { echo "no -- before the input" >&2; exit 3; }
[ -f "$5" ] || { echo "input is not a file" >&2; exit 4; }
mkdir -p "$out/htdemucs/track"
echo v > "$out/htdemucs/track/vocals.wav"
echo b > "$out/htdemucs/track/no_vocals.wav"
printf '%s\\n' "$@" > "$out/argv.txt"
"""


def _fake_binary(tmp_path: Path, body: str = FAKE_DEMUCS) -> str:
    path = tmp_path / "fake-demucs"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_demucs_runs_without_a_shell_and_a_dash_leading_path_is_a_file(tmp_path: Path) -> None:
    audio = tmp_path / "-rf evil;touch pwned.wav"
    audio.write_bytes(b"RIFF")
    work_dir, vocals, background = DemucsSeparator(_fake_binary(tmp_path)).separate(audio)
    try:
        assert vocals.read_text().strip() == "v" and background.read_text().strip() == "b"
        argv = (work_dir / "argv.txt").read_text().splitlines()
        assert argv[argv.index("--") + 1] == str(audio)
        assert not (Path.cwd() / "pwned.wav").exists()
    finally:
        import shutil

        shutil.rmtree(work_dir)


def test_demucs_work_dir_is_a_fresh_temp_dir_not_derived_from_the_input(tmp_path: Path) -> None:
    audio = tmp_path / "7312345678901234567.wav"
    audio.write_bytes(b"RIFF")
    sep = DemucsSeparator(_fake_binary(tmp_path))
    first, *_ = sep.separate(audio)
    second, *_ = sep.separate(audio)
    try:
        assert first != second
        assert "7312345678901234567" not in str(first)
    finally:
        import shutil

        shutil.rmtree(first)
        shutil.rmtree(second)


def test_demucs_failure_is_permanent_and_leaves_no_work_dir(tmp_path: Path) -> None:
    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("demucs-*"))
    binary = _fake_binary(tmp_path, "#!/bin/sh\nexit 1\n")
    with pytest.raises(PermanentProviderError):
        DemucsSeparator(binary).separate(tmp_path / "missing.wav")
    assert set(Path(tempfile.gettempdir()).glob("demucs-*")) == before


def test_demucs_timeout_is_transient(tmp_path: Path) -> None:
    binary = _fake_binary(tmp_path, "#!/bin/sh\nexec sleep 5\n")
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    with pytest.raises(TransientProviderError):
        DemucsSeparator(binary, timeout_seconds=0.3).separate(audio)


def test_demucs_missing_binary_is_permanent(tmp_path: Path) -> None:
    with pytest.raises(PermanentProviderError):
        DemucsSeparator(str(tmp_path / "nope")).separate(tmp_path / "a.wav")


# --- llm --------------------------------------------------------------------


def test_model_for_uses_the_configured_base_url_and_strips_the_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("LOCALIZER_MODEL", "openai:deepseek/deepseek-v4-pro")
    model = model_for("localizer", Settings())
    assert model.model_name == "deepseek/deepseek-v4-pro"
    assert str(model.base_url).startswith("http://127.0.0.1:9/v1")


def test_model_for_requires_a_model_and_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="DUBBER_MODEL"):
        model_for("dubber", Settings())
    monkeypatch.setenv("DUBBER_MODEL", "m")
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        model_for("dubber", Settings())


def test_environment_is_not_mutated_by_loading_settings() -> None:
    before = dict(os.environ)
    Settings()
    assert dict(os.environ) == before
