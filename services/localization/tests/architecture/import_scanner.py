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
    "localization.contracts",
    "localization.text",
    "localization.agents.temporal",
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
)

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


def _matches(name: str, roots: tuple[str, ...]) -> bool:
    return any(name == root or name.startswith(root + ".") for root in roots)


def _module_file(src_root: Path, dotted: str) -> Path | None:
    """The first-party file a dotted name resolves to, trying shorter prefixes so
    `from localization.contracts import X` finds `contracts/__init__.py`."""
    parts = dotted.split(".")
    for end in range(len(parts), 0, -1):
        base = src_root.joinpath(*parts[:end])
        if base.with_suffix(".py").is_file():
            return base.with_suffix(".py")
        if (base / "__init__.py").is_file():
            return base / "__init__.py"
    return None


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
    stack = [entry]
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in _imports(path, _package_of(src_root, path)):
            if _matches(name, forbidden):
                violations.append(
                    Violation(str(path.relative_to(src_root)), name, "forbidden dependency")
                )
            if _matches(name, (FIRST_PARTY,)):
                target = _module_file(src_root, name)
                if target is not None:
                    stack.append(target)
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
