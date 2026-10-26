"""Workflow code stays deterministic: it may import only an allowlist, and nothing
it reaches through first-party modules may touch I/O, the clock, randomness, env
or a provider."""

from pathlib import Path

import pytest
from tests.architecture.import_scanner import scan_workflows

SRC = Path(__file__).resolve().parents[2] / "src"


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _rel(key: str) -> str:
    dotted = key.replace("____init__", "/@init@").replace("__", "/").replace("@init@", "__init__")
    return dotted + ".py"


def _tree(tmp_path: Path, workflow_body: str, **extra: str) -> tuple[Path, Path]:
    src = tmp_path / "src"
    for pkg in ("localization", "localization/workflows", "localization/agents"):
        _write(src, f"{pkg}/__init__.py", "")
    _write(src, "localization/workflows/flow.py", workflow_body)
    for rel, body in extra.items():
        _write(src, _rel(rel), body)
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


def test_a_parent_packages_init_is_executed_so_it_is_scanned(tmp_path: Path) -> None:
    # `from localization.contracts.sub import X` runs contracts/__init__.py first.
    src, wf = _tree(
        tmp_path,
        "from localization.contracts.sub import X\n",
        localization__contracts____init__="import os\n",
        localization__contracts__sub="X = 1\n",
    )
    _, violations = scan_workflows(src, wf)
    assert any(
        v.import_name == "os" and v.file.endswith("contracts/__init__.py") for v in violations
    ), violations


def test_a_deeply_nested_import_scans_every_parent_init(tmp_path: Path) -> None:
    src, wf = _tree(
        tmp_path,
        "import localization.contracts.a.b.c\n",
        localization__contracts____init__="",
        localization__contracts__a____init__="import socket\n",
        localization__contracts__a__b____init__="",
        localization__contracts__a__b__c="",
    )
    _, violations = scan_workflows(src, wf)
    assert any(v.import_name == "socket" for v in violations), violations


@pytest.mark.parametrize(
    "snippet, name",
    [
        ('__import__("os")', "__import__()"),
        ('exec("import os")', "exec()"),
        ('eval("1")', "eval()"),
        ('compile("1", "f", "eval")', "compile()"),
        ('open("/etc/passwd")', "open()"),
        ("__builtins__", "__builtins__"),
    ],
)
def test_dynamic_escapes_from_the_allowlist_are_rejected(
    tmp_path: Path, snippet: str, name: str
) -> None:
    src, wf = _tree(tmp_path, f"x = {snippet}\n")
    _, violations = scan_workflows(src, wf)
    assert name in {v.import_name for v in violations}, violations


def test_dynamic_escape_in_a_module_reached_through_an_allowed_import(tmp_path: Path) -> None:
    src, wf = _tree(
        tmp_path,
        "from localization.contracts import helper\n",
        localization__contracts____init__="",
        localization__contracts__helper='m = __import__("subprocess")\n',
    )
    _, violations = scan_workflows(src, wf)
    assert "__import__()" in {v.import_name for v in violations}


@pytest.mark.parametrize(
    "module",
    [
        "pydantic_settings",
        "dotenv",
        "pathlib",
        "tempfile",
        "shutil",
        "urllib.request",
        "asyncio",
        "threading",
        "uuid",
        "psycopg",
        "importlib",
    ],
)
def test_allowed_modules_cannot_reach_io_helpers(tmp_path: Path, module: str) -> None:
    src, wf = _tree(
        tmp_path,
        "from localization.contracts import helper\n",
        localization__contracts____init__="",
        localization__contracts__helper=f"import {module}\n",
    )
    _, violations = scan_workflows(src, wf)
    assert module in {v.import_name for v in violations}, violations


def test_the_payload_precheck_is_usable_from_workflow_code(tmp_path: Path) -> None:
    real = (SRC / "localization" / "contracts" / "limits.py").read_text()
    src, wf = _tree(
        tmp_path,
        "from localization.contracts.limits import ensure_payload_fits\n",
        localization__contracts____init__="",
        localization__contracts__limits=real,
    )
    _, violations = scan_workflows(src, wf)
    assert violations == []


def test_relative_import_cannot_smuggle_a_module_past_the_allowlist(tmp_path: Path) -> None:
    src, wf = _tree(
        tmp_path,
        "from . import sneaky\n",
        localization__workflows__sneaky="import os\n",
    )
    _, violations = scan_workflows(src, wf)
    assert any(v.import_name == "os" for v in violations)
