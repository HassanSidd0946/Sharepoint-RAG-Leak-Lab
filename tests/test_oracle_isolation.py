"""The oracle must stay independent of every engine (CLAUDE.md rule 2).

Two checks: (1) a static scan of every import in src/leaklab/oracle/ against an allow-list; (2) a
runtime check in a fresh interpreter that importing leaklab.oracle loads no engine module and no
HTTP/auth library, directly or indirectly.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ORACLE_DIR = Path(__file__).resolve().parents[1] / "src" / "leaklab" / "oracle"

# The only first-party modules the oracle may use.
ALLOWED_FIRST_PARTY = ("leaklab.spec", "leaklab.oracle")
FORBIDDEN_FIRST_PARTY = (
    "leaklab.setup_a",
    "leaklab.setup_b",
    "leaklab.graph_client",
    "leaklab.sp_rest",
    "leaklab.auth",
    "leaklab.config",
    "leaklab.http_retry",
    "leaklab.generator",
    "leaklab.runner",
    "leaklab.agent",
    "leaklab.report",
)
FORBIDDEN_THIRD_PARTY = ("httpx", "msal", "requests", "urllib3", "typer", "rich")


def imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{path.name}: relative imports hide dependencies"
            module = node.module or ""
            names.append(module)
            names += [f"{module}.{alias.name}" for alias in node.names]
    return names


def oracle_files() -> list[Path]:
    files = sorted(ORACLE_DIR.glob("*.py"))
    assert files, "no oracle modules found"
    return files


@pytest.mark.parametrize("path", oracle_files(), ids=lambda p: p.name)
def test_oracle_imports_only_allowed_first_party(path: Path) -> None:
    for name in imported_modules(path):
        if name.startswith("leaklab"):
            assert name.startswith(ALLOWED_FIRST_PARTY), f"{path.name} imports {name}"


@pytest.mark.parametrize("path", oracle_files(), ids=lambda p: p.name)
def test_oracle_has_no_forbidden_imports(path: Path) -> None:
    for name in imported_modules(path):
        assert not name.startswith(FORBIDDEN_FIRST_PARTY), f"{path.name} imports {name}"
        assert name.split(".")[0] not in FORBIDDEN_THIRD_PARTY, f"{path.name} imports {name}"


def test_importing_the_oracle_loads_no_engine_code() -> None:
    forbidden = (*FORBIDDEN_FIRST_PARTY, *FORBIDDEN_THIRD_PARTY)
    src = str(ORACLE_DIR.parents[1])  # src/: test the working tree, not an installed copy
    code = (
        "import sys\n"
        f"sys.path.insert(0, {src!r})\n"
        "import leaklab.oracle\n"
        f"forbidden = {forbidden!r}\n"
        "loaded = sorted(m for m in sys.modules if m.startswith(forbidden))\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "", f"importing leaklab.oracle also loaded: {result.stdout}"
