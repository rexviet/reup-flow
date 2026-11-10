import pytest

from localization.review_bridge.visual import apply_visual, capture_visual, require_visual_only


def test_native_editor_ids_and_boolean_serialization_preserve_source_meaning():
    before = b"<main data-no-timeline><video muted playsinline controls></video></main>"
    after = (
        b'<main data-hf-id="hf-ab12" data-no-timeline="">'
        b'<video data-hf-id="hf-cd34" muted playsinline controls></video></main>'
    )
    require_visual_only(after, before, [])
    with pytest.raises(ValueError):
        require_visual_only(after.replace(b"<video ", b"<video autoplay "), before, [])
    with pytest.raises(ValueError):
        require_visual_only(after.replace(b"hf-ab12", b"invalid-id"), before, [])


def test_literal_native_gradient_survives_regeneration_without_allowing_urls():
    from types import SimpleNamespace

    cues = [SimpleNamespace(id=5)]
    baseline = b'<div id="cue-5" class="caption">Text</div>'
    gradient = 'linear-gradient(135deg, rgba(0, 0, 0, 0) 0%, rgba(255, 255, 255, 0.04) 100%)'
    edited = baseline.replace(b'class=', f'style="background-image: {gradient}" class='.encode())
    require_visual_only(edited, baseline, cues)
    visual = capture_visual(edited, cues)
    assert gradient in apply_visual(baseline, visual).decode()
    with pytest.raises(ValueError, match="unsupported visual value"):
        capture_visual(edited.replace(gradient.encode(), b'url(https://example.com/image)'), cues)
