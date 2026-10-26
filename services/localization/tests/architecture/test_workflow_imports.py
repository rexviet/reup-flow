"""Workflow code stays deterministic: it may import only an allowlist, and nothing
it reaches through first-party modules may touch I/O, the clock, randomness, env
or a provider."""

from pathlib import Path

from tests.architecture.import_scanner import scan_workflows

SRC = Path(__file__).resolve().parents[2] / "src"


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _tree(tmp_path: Path, workflow_body: str, **extra: str) -> tuple[Path, Path]:
    src = tmp_path / "src"
    for pkg in ("localization", "localization/workflows", "localization/agents"):
        _write(src, f"{pkg}/__init__.py", "")
    _write(src, "localization/workflows/flow.py", workflow_body)
    for rel, body in extra.items():
        _write(src, rel.replace("__", "/") + ".py", body)
    return src, src / "localization" / "workflows"


def test_real_workflows_import_only_the_allowlist() -> None:
    scanned, violations = scan_workflows(SRC, SRC / "localization" / "workflows")
    assert scanned >= 1, "the scanner found no workflow modules; it proves nothing"
    assert violations == []


def test_allowed_imports_pass(tmp_path: Path) -> None:
    src, wf = _tree(
        tmp_path,
        "from datetime import timedelta\n"
        "from temporalio import workflow\n"
        "from localization.contracts import X\n",
        localization__contracts="X = 1\n",
    )
    _, violations = scan_workflows(src, wf)
    assert violations == []


def test_direct_forbidden_import_is_caught(tmp_path: Path) -> None:
    for module in ("os", "httpx", "boto3", "random", "time"):
        src, wf = _tree(tmp_path, f"import {module}\n")
        _, violations = scan_workflows(src, wf)
        assert {v.import_name for v in violations} == {module}, module


def test_import_of_providers_and_storage_is_caught(tmp_path: Path) -> None:
    src, wf = _tree(
        tmp_path,
        "from localization.providers import openai_tts\n"
        "from localization.storage.object_store import work_key\n",
        localization__providers__openai_tts="",
        localization__storage__object_store="",
    )
    _, violations = scan_workflows(src, wf)
    assert {v.import_name for v in violations} >= {
        "localization.providers.openai_tts",
        "localization.storage.object_store.work_key",
    }


def test_control_boto3_hidden_behind_agents_is_caught(tmp_path: Path) -> None:
    src, wf = _tree(
        tmp_path,
        "from localization.agents import temporal\n",
        localization__agents__temporal="from localization.agents import helper\n",
        localization__agents__helper="import boto3\n",
    )
    _, violations = scan_workflows(src, wf)
    assert any(
        v.import_name == "boto3" and v.file.endswith("agents/helper.py") for v in violations
    ), violations


def test_relative_import_cannot_smuggle_a_module_past_the_allowlist(tmp_path: Path) -> None:
    src, wf = _tree(
        tmp_path,
        "from . import sneaky\n",
        localization__workflows__sneaky="import os\n",
    )
    _, violations = scan_workflows(src, wf)
    assert any(v.import_name == "os" for v in violations)
