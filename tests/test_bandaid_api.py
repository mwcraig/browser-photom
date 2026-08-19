"""Guard against ballet_sgemm.py importing names bandaid.ballet no longer has.

The host test env deliberately does not install bandaid (or eloy, which
bandaid.ballet imports from), and `pixi run test` must never pull in heavy
deps like astropy -- so this cannot just `import bandaid.ballet` and check
`hasattr`. bandaid-src/src/bandaid/ballet.py does `from eloy.ballet.model
import load_weights_file`, and eloy is not installed here either, so even
loading ballet.py alone via importlib would fail; the names are recovered by
parsing both files' ASTs instead, which needs neither package importable.

Skipped entirely unless the bandaid-src checkout (fetched by the
`fetch-bandaid` pixi task, branch numpy-ballet) is present on disk -- CI/dev
machines that haven't run that task get no signal either way, rather than a
false failure.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BALLET_SGEMM_PY = REPO_ROOT / "content" / "ballet_sgemm.py"
BANDAID_BALLET_PY = REPO_ROOT / "bandaid-src" / "src" / "bandaid" / "ballet.py"

pytestmark = pytest.mark.skipif(
    not BANDAID_BALLET_PY.exists(),
    reason="bandaid-src checkout not present (run `pixi run fetch-bandaid`)",
)


def _names_imported_from_bandaid_ballet(source_path):
    """Names ballet_sgemm.py's `from bandaid.ballet import (...)` pulls in."""
    tree = ast.parse(source_path.read_text())
    names = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "bandaid.ballet"
            and node.level == 0
        ):
            names.update(alias.name for alias in node.names)
    return names


def _top_level_names(source_path):
    """Names bound at module scope: def/class names, assignment targets.

    Only `tree.body` (not `ast.walk`) so a name defined inside a function or
    class body -- not importable from the module -- is not mistaken for one
    that is.
    """
    tree = ast.parse(source_path.read_text())
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name) for alias in node.names)
    return names


def test_bandaid_ballet_exports_everything_ballet_sgemm_imports():
    required = _names_imported_from_bandaid_ballet(BALLET_SGEMM_PY)
    # Guards the guard: an ast.walk that stopped matching the import (e.g. the
    # import got reworded) must fail loudly, not pass vacuously on an empty set.
    assert required, "found no `from bandaid.ballet import (...)` in ballet_sgemm.py"

    available = _top_level_names(BANDAID_BALLET_PY)
    missing = required - available
    assert not missing, (
        f"bandaid.ballet ({BANDAID_BALLET_PY}) no longer defines {sorted(missing)}, "
        f"which content/ballet_sgemm.py imports from it"
    )
