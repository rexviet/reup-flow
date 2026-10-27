from pathlib import Path

from localization.config import DEFAULT_CAPTION_FONT, default_caption_font

FONT = Path("assets/fonts/BeVietnamPro-SemiBold.ttf")


def test_a_source_checkout_finds_the_font_in_the_repository_assets():
    here = Path("/work/repo/services/localization/src/localization/config.py")

    assert default_caption_font(here) == Path("/work/repo") / FONT


def test_the_container_layout_falls_back_to_the_mounted_assets_directory():
    here = Path("/app/src/localization/config.py")

    assert default_caption_font(here) == Path("/") / FONT


def test_the_default_for_this_checkout_is_the_real_font():
    assert DEFAULT_CAPTION_FONT.is_file()
