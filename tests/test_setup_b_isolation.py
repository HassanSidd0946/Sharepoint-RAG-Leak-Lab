"""Setup B must never import the oracle (the oracle is a test-only referee, CLAUDE.md rule 2).

The mirror image of tests/test_oracle_isolation.py: that one keeps the oracle free of engines, this
one keeps the engine free of the oracle. A static scan of every import under src/leaklab/setup_b and
setup_a, and a check that the password of the database never sits in docker-compose.yml.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETUP_B = ROOT / "src" / "leaklab" / "setup_b"
SETUP_A = ROOT / "src" / "leaklab" / "setup_a"


def imported(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{path.name}: relative imports hide dependencies"
            module = node.module or ""
            names.append(module)
            names += [f"{module}.{a.name}" for a in node.names]
    return names


@pytest.mark.parametrize("package", [SETUP_B, SETUP_A], ids=["setup_b", "setup_a"])
def test_no_module_of_the_engines_imports_the_oracle(package: Path) -> None:
    files = sorted(package.rglob("*.py"))
    assert files
    for path in files:
        bad = [
            n for n in imported(path) if n == "leaklab.oracle" or n.startswith("leaklab.oracle.")
        ]
        assert not bad, f"{path.relative_to(ROOT)} imports the oracle: {bad}"


def test_setup_b_state_does_not_import_the_spec_either() -> None:
    """The state knows tenants only through Graph and SharePoint, never through tenant_spec.yaml."""
    for path in sorted(SETUP_B.rglob("*.py")):
        bad = [n for n in imported(path) if n.startswith("leaklab.spec")]
        assert not bad, f"{path.relative_to(ROOT)} imports the spec: {bad}"


def test_the_compose_file_holds_no_password() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "${LEAKLAB_PG_PASSWORD:?" in text  # taken from the environment, and required
    assert "127.0.0.1:5433:5432" in text  # not exposed beyond the machine
    for line in text.splitlines():
        if "POSTGRES_PASSWORD" in line:
            assert "${" in line
