"""Compatibility fixture contracts; no provider or production persistence calls."""

import copy
import http.client
import importlib.util
import json
import subprocess
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

SPIKE = Path(__file__).resolve().parents[2] / "spikes/hyperframes-review"
spec = importlib.util.spec_from_file_location("review_adapter", SPIKE / "review_adapter.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


@pytest.fixture
def document():
    return json.loads((SPIKE / "fixtures/cues.json").read_text())


def test_stable_style_merges_into_latest_text_and_regenerates(document, tmp_path):
    document["cues"][0]["text"] = "Câu đầu nay có nhiều từ hơn rất nhiều."
    document["cues"][2]["text"] = "Caption mới; audio cũ chưa đồng bộ."
    updated = adapter.merge_visual(document, {"cue-later": {"color": "#00ff00"}})
    adapter.generate(updated, tmp_path)
    assert updated["cues"][2]["style"]["color"] == "#00ff00"
    assert updated["cues"][2]["text"] == document["cues"][2]["text"]
    assert 'id="cue-later"' in (tmp_path / "index.html").read_text()
    assert "Caption mới" in (tmp_path / "captions.srt").read_text()


def test_srt_roundtrip_loses_sequence_and_ids_but_keeps_blank_sidecar(document):
    srt = adapter.export_srt(document)
    assert "cue-" not in srt
    assert "Unresolved" not in srt
    restored = adapter.import_srt(document, srt.replace("1\n", "99\n", 1))
    assert restored["cues"] == document["cues"]
    assert restored["cues"][1]["text"] == ""


def test_srt_import_preserves_payload_whitespace(document):
    document["cues"][0]["text"] = "  padded text  "
    document["cues"][2]["text"] = "first line\nsecond line  "
    restored = adapter.import_srt(document, adapter.export_srt(document))
    assert restored["cues"][0]["text"] == "  padded text  "
    assert restored["cues"][2]["text"] == "first line\nsecond line  "


def test_ambiguous_missing_or_changed_srt_anchor_rejected(document):
    srt = adapter.export_srt(document)
    for changed in [
        srt.replace("00:00:02,000", "00:00:02,001"),
        srt.split("\n\n", 1)[1],
        srt + "\n" + srt.split("\n\n")[0],
    ]:
        with pytest.raises(ValueError):
            adapter.import_srt(document, changed)
    duplicate = copy.deepcopy(document["cues"][0])
    duplicate["cueId"] = "cue-duplicate"
    document["cues"].append(duplicate)
    with pytest.raises(ValueError, match="ambiguous"):
        adapter.import_srt(document, srt)


@pytest.mark.parametrize("name", ["/tmp/escape", "../escape", "a/../../escape", "a\\escape"])
def test_path_escape_rejected(tmp_path, name):
    with pytest.raises(ValueError):
        adapter.project_path(tmp_path, name)


def test_symlink_escape_rejected(tmp_path):
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        adapter.project_path(tmp_path, "link/escape")


@pytest.mark.parametrize(
    "visual",
    [
        {"w9": {"color": "#ffffff"}},
        {"cue-later": {"text": "injected"}},
        {"cue-later": {"color": "url(https://evil)"}},
    ],
)
def test_visual_allowlist(document, visual):
    with pytest.raises(ValueError):
        adapter.merge_visual(document, visual)


def test_literal_payload_and_source_mask_without_vietnamese(document, tmp_path):
    adapter.generate(document, tmp_path)
    output = (tmp_path / "index.html").read_text()
    assert "<img src=x" not in output
    assert "&lt;img src=x" in output
    assert 'id="mask-cue-blank"' in output
    assert 'id="cue-blank"' not in output
    assert "height:86px" in output
    assert "Content-Security-Policy" in output
    assert json.loads((tmp_path / "cues.json").read_text()) == document
    document["source_region"] = None
    adapter.generate(document, tmp_path)
    assert 'class="clip source-mask"' not in (tmp_path / "index.html").read_text()


def test_invalid_srt_generation_leaves_no_partial_artifacts(document, tmp_path):
    document["cues"][0]["text"] = "invalid\n\npayload"
    output = tmp_path / "project"
    with pytest.raises(ValueError, match="SRT cannot preserve"):
        adapter.generate(document, output)
    assert not output.exists()


def test_bridge_rejects_host_origin_and_stale_revision(document, tmp_path):
    adapter.generate(document, tmp_path)
    # Bind a test-only ephemeral port, then use the exact authority in authorization.
    server = adapter.bridge(tmp_path, 0)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for host, origin, expected in [
            ("evil.test", None, 403),
            (f"127.0.0.1:{port}", "https://evil.test", 403),
            (f"127.0.0.1:{port}", f"http://127.0.0.1:{port}", 200),
            (f"127.0.0.1:{port}", None, 409),
        ]:
            request = Request(
                f"http://127.0.0.1:{port}/visual",
                method="POST",
                data=json.dumps(
                    {"revision": 1, "visual": {"cue-later": {"color": "#00ff00"}}}
                ).encode(),
                headers={
                    "Host": host,
                    "Content-Type": "application/json",
                    **({"Origin": origin} if origin else {}),
                },
            )
            try:
                with urlopen(request) as response:
                    status = response.status
            except HTTPError as exc:
                status = exc.code
            assert status == expected
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_bridge_rejects_malformed_content_length(document, tmp_path):
    adapter.generate(document, tmp_path)
    server = adapter.bridge(tmp_path, 0)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port)
        connection.putrequest("POST", "/visual")
        connection.putheader("Host", f"127.0.0.1:{port}")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "invalid")
        connection.endheaders()
        assert connection.getresponse().status == 400
        connection.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def make_clip(path, seconds):
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            str(path),
        ],
        check=True,
    )


@pytest.mark.parametrize(
    "seconds, active, stretched",
    [(0.8, True, False), (1.10, True, True), (1.15, True, True), (1.16, False, False)],
)
def test_revoice_measures_and_actually_stretches(tmp_path, seconds, active, stretched):
    old, candidate, output = (tmp_path / name for name in ("old.wav", "new.wav", "fit.wav"))
    make_clip(old, 0.7)
    make_clip(candidate, seconds)
    original = old.read_bytes()
    cue = {
        "text": "Câu mới",
        "start_ms": 0,
        "end_ms": 1000,
        "active_clip": str(old),
        "spoken_text": "Câu cũ",
    }
    result = adapter.revoice(cue, candidate, output)
    assert result["mismatch"] is not active
    assert result["text"] == "Câu mới"
    assert old.read_bytes() == original
    assert output.exists() is stretched
    if active:
        assert adapter.duration_ms(Path(result["active_clip"])) <= 1050
        assert result["spoken_text"] == "Câu mới"
    else:
        assert result["active_clip"] == str(old)
        assert result["spoken_text"] == "Câu cũ"


def test_revoice_failure_and_blank_keep_active(tmp_path):
    cue = {"text": "draft", "start_ms": 0, "end_ms": 1000, "active_clip": "old.wav"}
    assert adapter.revoice(cue, tmp_path / "missing.wav", tmp_path / "fit.wav") == {
        **cue,
        "mismatch": True,
    }
    del cue["active_clip"]
    assert "active_clip" not in adapter.revoice(cue, tmp_path / "missing.wav", tmp_path / "fit.wav")


def test_capture_studio_style_before_canonical_regeneration(document, tmp_path):
    adapter.generate(document, tmp_path)
    source = tmp_path / "index.html"
    source.write_text(source.read_text().replace("font-size:36px", "font-size:42px"))
    document["cues"][0]["text"] = "Earlier cue with a different word count."
    updated = adapter.regenerate(document, tmp_path)
    assert updated["cues"][2]["style"]["font-size"] == "42px"
    assert "Earlier cue with a different word count." in source.read_text()
    assert "font-size:42px" in source.read_text()


def test_failed_actual_stretch_keeps_old_clip_and_draft(tmp_path):
    old, candidate, output = (tmp_path / name for name in ("old.wav", "new.wav", "fit.wav"))
    make_clip(old, 0.7)
    make_clip(candidate, 1.1)
    output.write_bytes(b"already exists; ffmpeg -n must refuse replacement")
    cue = {
        "text": "new draft",
        "start_ms": 0,
        "end_ms": 1000,
        "active_clip": str(old),
        "spoken_text": "old text",
    }
    result = adapter.revoice(cue, candidate, output)
    assert result == {**cue, "mismatch": True}
    assert output.read_bytes() == b"already exists; ffmpeg -n must refuse replacement"
