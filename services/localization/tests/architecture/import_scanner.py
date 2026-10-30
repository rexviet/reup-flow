"""AST import scanner for the architecture tests.

`scan_workflows` checks every module under `workflows/` against an allowlist, and
follows first-party imports so a forbidden dependency hidden one hop away (for
example `boto3` inside `agents/`) is still found. `scan_forbidden_reach` does the
same walk for modules that must not reach a given set of roots.
"""

import ast
from dataclasses import dataclass
from pathlib import Path

ALLOWED = (
    "__future__",
    "temporalio.workflow",
    # Pure data: retry policies and the exception types a workflow catches.
    "temporalio.common",
    "temporalio.exceptions",
    "collections.abc",
    "localization.contracts",
    "localization.text",
    "localization.agents.temporal",
    # Pure: which stage a failed review sends a run back to.
    "localization.workflows.review_routing",
    # The other workflow's queues, retry policies and pure helpers, shared by the Vbee
    # workflow. It is a workflow module itself, scanned against these same rules.
    "localization.workflows.video_localization",
    "datetime.timedelta",
)

# Reaching any of these from workflow code (directly or through first-party
# modules) breaks determinism or the no-I/O rule.
FORBIDDEN_REACH = (
    "os",
    "time",
    "random",
    "subprocess",
    "socket",
    "httpx",
    "requests",
    "boto3",
    "botocore",
    "localization.providers",
    "localization.storage",
    "localization.config",
    # Reading env/files, clocks, threads and ids, or loading code by name.
    "pydantic_settings",
    "dotenv",
    "pathlib",
    "tempfile",
    "shutil",
    "urllib",
    "asyncio",
    "threading",
    "uuid",
    "psycopg",
    "importlib",
    "builtins",
    "ctypes",
    "pickle",
)

# Names that load or run code, or open files, without an import statement. Calling
# `__import__("os")` or `exec(...)` walks around any import allowlist.
FORBIDDEN_CALLS = frozenset({"__import__", "eval", "exec", "compile", "open", "input"})
FORBIDDEN_NAMES = frozenset({"__builtins__", "__loader__"})

FIRST_PARTY = "localization"


@dataclass(frozen=True)
class Violation:
    file: str
    import_name: str
    reason: str


def _imports(path: Path, package: str) -> list[str]:
    """Fully qualified candidate names imported by `path`."""
    tree = ast.parse(path.read_text(), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".")
                base = ".".join(base_parts[: len(base_parts) - (node.level - 1)])
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            names.extend(f"{module}.{alias.name}" for alias in node.names)
    return names


def _dynamic_escapes(path: Path) -> list[str]:
    """Constructs in `path` that bypass the import allowlist."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else None
            if name in FORBIDDEN_CALLS:
                found.append(f"{name}()")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(node.id)
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            found.append(node.attr)
    return found


def _matches(name: str, roots: tuple[str, ...]) -> bool:
    return any(name == root or name.startswith(root + ".") for root in roots)


def _module_files(src_root: Path, dotted: str) -> list[Path]:
    """Every first-party file importing `dotted` executes: the module itself (or the
    longest existing prefix, so `from pkg import name` finds `pkg/__init__.py`)
    AND the `__init__.py` of each parent package, which Python runs first."""
    parts = dotted.split(".")
    target: Path | None = None
    end = len(parts)
    while end > 0 and target is None:
        base = src_root.joinpath(*parts[:end])
        if base.with_suffix(".py").is_file():
            target = base.with_suffix(".py")
        elif (base / "__init__.py").is_file():
            target = base / "__init__.py"
        else:
            end -= 1
    if target is None:
        return []
    files = [target]
    for i in range(1, end):
        init = src_root.joinpath(*parts[:i]) / "__init__.py"
        if init.is_file():
            files.append(init)
    return files


def _package_of(src_root: Path, path: Path) -> str:
    rel = path.relative_to(src_root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        return ".".join(parts[:-1]) or FIRST_PARTY
    return ".".join(parts[:-1])


def scan_forbidden_reach(
    src_root: Path, entry: Path, forbidden: tuple[str, ...] = FORBIDDEN_REACH
) -> list[Violation]:
    """Violations reachable from `entry` through first-party imports."""
    violations: list[Violation] = []
    seen: set[Path] = set()
    stack = [entry, *_module_files(src_root, _package_of(src_root, entry))]
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        for escape in _dynamic_escapes(path):
            violations.append(
                Violation(str(path.relative_to(src_root)), escape, "bypasses the import allowlist")
            )
        for name in _imports(path, _package_of(src_root, path)):
            if _matches(name, forbidden):
                violations.append(
                    Violation(str(path.relative_to(src_root)), name, "forbidden dependency")
                )
            if _matches(name, (FIRST_PARTY,)):
                stack.extend(_module_files(src_root, name))
    return violations


def scan_workflows(src_root: Path, workflows_dir: Path) -> tuple[int, list[Violation]]:
    """Returns `(files_scanned, violations)` for every module under `workflows_dir`."""
    violations: list[Violation] = []
    files = sorted(workflows_dir.rglob("*.py"))
    for path in files:
        for name in _imports(path, _package_of(src_root, path)):
            if not _matches(name, ALLOWED):
                violations.append(
                    Violation(str(path.relative_to(src_root)), name, "not in the allowlist")
                )
        violations.extend(scan_forbidden_reach(src_root, path))
    return len(files), violations
