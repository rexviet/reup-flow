"""What the image must keep: pinned base, verified weights, safe loader, no root."""

import re
from pathlib import Path

DOCKERFILE = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()


def _instructions() -> list[str]:
    """Dockerfile instructions with their line continuations joined."""
    return [
        line.strip()
        for line in re.sub(r"\\\n", " ", DOCKERFILE).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_every_base_image_is_pinned_by_digest() -> None:
    froms = [i for i in _instructions() if i.startswith(("FROM ", "COPY --from="))]
    assert froms, "the Dockerfile has no FROM"
    for line in froms:
        assert re.search(r"@sha256:[0-9a-f]{64}", line), line


def test_the_demucs_weights_are_verified_against_a_full_sha256_or_the_build_fails() -> None:
    declared = re.search(r"ARG DEMUCS_WEIGHTS_SHA256=([0-9a-f]{64})\b", DOCKERFILE)
    assert declared, "the expected sha256 of the weights is not pinned"
    download = next(i for i in _instructions() if "curl" in i and "checkpoints" in i)
    # `sha256sum -c` exits non-zero on a mismatch and `&&` carries that to the build.
    assert re.search(r'&&\s*echo "\$DEMUCS_WEIGHTS_SHA256\s+\S+"\s*\|\s*sha256sum -c -', download)
    assert not re.search(r"\|\|\s*(true|:)", download)


def test_demucs_runs_through_the_weights_only_launcher() -> None:
    instructions = _instructions()
    assert any(
        "localization.providers.demucs_safe" in i and "demucs-safe" in i for i in instructions
    )
    assert "ENV DEMUCS_BIN=/usr/local/bin/demucs-safe" in instructions


def test_the_image_installs_ffmpeg_and_does_not_run_as_root() -> None:
    instructions = _instructions()
    assert any(i.startswith("RUN apt-get") and "ffmpeg" in i for i in instructions)
    users = [i for i in instructions if i.startswith("USER ")]
    assert users and users[-1] != "USER root"


def test_every_worker_that_measures_captions_has_the_font_where_its_setting_says() -> None:
    """The compose file is what decides the path inside a container: a font found on a
    developer's checkout says nothing about it. The agents worker once started without
    the font and failed its first review after the render."""
    import yaml

    root = Path(__file__).resolve().parents[3]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    for name in ("worker-agents", "worker-media"):
        service = compose["services"][name]
        setting = service["environment"]["CAPTION_FONT_PATH"]
        path = setting.removeprefix("${CAPTION_FONT_PATH:-").removesuffix("}")
        mounts = [v.split(":")[:2] for v in service["volumes"]]
        assert any(
            path.startswith(target.rstrip("/") + "/") and (root / source).is_dir()
            for source, target in mounts
        ), f"{name} has no mount that holds {path}"
        assert (root / "assets/fonts" / Path(path).name).is_file()
