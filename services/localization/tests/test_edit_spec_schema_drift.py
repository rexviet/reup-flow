"""The renderer reads `contracts/edit-spec.schema.json`; the Pydantic model is the
source. The stored file must equal what the model generates today."""

import json

from localization.contracts.export import (
    EDIT_SPEC_SCHEMA_PATH,
    main,
    render_edit_spec_schema,
)


def test_stored_schema_matches_the_model() -> None:
    assert EDIT_SPEC_SCHEMA_PATH.exists(), "run python -m localization.contracts.export"
    assert json.loads(EDIT_SPEC_SCHEMA_PATH.read_text()) == json.loads(render_edit_spec_schema())


def test_check_mode_reports_a_stale_file(tmp_path, monkeypatch) -> None:
    stale = tmp_path / "edit-spec.schema.json"
    stale.write_text("{}\n")
    monkeypatch.setattr("localization.contracts.export.EDIT_SPEC_SCHEMA_PATH", stale)
    assert main(["--check"]) == 1
    assert main([]) == 0
    assert main(["--check"]) == 0
