"""One translation only: audio synthesis, caption layout and audio assembly consume
the `LocalizedScript`; they never produce or alter text, so they must not reach
an agent or pydantic-ai."""

from pathlib import Path

import pytest
from tests.architecture.import_scanner import scan_forbidden_reach

SRC = Path(__file__).resolve().parents[2] / "src"
ACTIVITIES = SRC / "localization" / "activities"
NO_TRANSLATION = ("localization.agents", "pydantic_ai")
STAGES = ("synthesize", "layout_captions", "assemble_audio")


@pytest.mark.parametrize("stage", STAGES)
def test_stage_does_not_reach_an_agent(stage: str) -> None:
    path = ACTIVITIES / f"{stage}.py"
    if not path.exists():
        pytest.skip(f"{stage} is not implemented yet")
    assert scan_forbidden_reach(SRC, path, NO_TRANSLATION) == []


def test_control_a_stage_importing_an_agent_is_caught(tmp_path: Path) -> None:
    src = tmp_path / "src"
    for rel, body in {
        "localization/__init__.py": "",
        "localization/agents/__init__.py": "",
        "localization/agents/localizer.py": "import pydantic_ai\n",
        "localization/activities/__init__.py": "",
        "localization/activities/synthesize.py": "from localization.agents import localizer\n",
    }.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    violations = scan_forbidden_reach(
        src, src / "localization/activities/synthesize.py", NO_TRANSLATION
    )
    names = {v.import_name for v in violations}
    assert "localization.agents.localizer" in names
    assert "pydantic_ai" in names
