"""Detection is deterministic, proven structurally rather than promised.

`tests/drift/test_compare.py` proves the *behaviour* -- the same two runs give
the same material moves with a narrative and without one. This file proves the
narrower structural fact that makes that behaviour hold for code nobody has
written yet: `core/drift/` cannot reach a model or a database, because it does
not import one.

`tests/test_boundaries.py` owns the frozen import-separation proofs for
`core/matcher/` and is not modified; this is the same technique applied to the
one new package, in its own file.
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DRIFT_DIR = REPO_ROOT / "core" / "drift"

#: What `core/drift/` may not import.
#:
#: `core.llm` -- the model writes `narrative` and nothing else, so the module
#: that decides `material` must not be able to call one. The narrative arrives
#: as an argument.
#:
#: `core.store` / `sqlmodel` / `sqlalchemy` -- §7: "core/drift/ takes plain
#: dicts and models as arguments and never touches the store itself, so it stays
#: testable without a database". Every test in this directory runs with no
#: database, and that is only guaranteed while this holds.
#:
#: `scorer` -- drift compares two `Metrics` that were already scored; a drift
#: module that could re-score would be a second place a metric is computed.
FORBIDDEN = ("core.llm", "core.store", "sqlmodel", "sqlalchemy", "scorer")


def _imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append(node.module or "")
            found += [alias.name for alias in node.names]
    return found


def test_drift_imports_neither_a_model_nor_a_database():
    paths = list(DRIFT_DIR.rglob("*.py"))
    assert paths, f"no .py files under {DRIFT_DIR}; the check would pass vacuously"
    for path in paths:
        for module in _imports(path):
            for banned in FORBIDDEN:
                assert not module.startswith(banned), (
                    f"{path.name} imports {module!r}: core/drift/ decides "
                    f"`material` and must not be able to reach {banned}"
                )


def test_every_threshold_is_a_named_module_constant():
    """§7: "Thresholds are named constants". A literal buried in a comparison
    is a threshold nobody can find, review, or change in one place."""
    from core.drift import compare as module

    constants = {
        name: value
        for name, value in vars(module).items()
        if name.isupper() and isinstance(value, float)
    }
    assert constants, "no named float threshold in core/drift/compare.py"

    source = ast.parse((DRIFT_DIR / "compare.py").read_text(encoding="utf-8"))
    thresholds = set(constants.values())
    for node in ast.walk(source):
        if isinstance(node, ast.Compare):
            for operand in [node.left, *node.comparators]:
                if isinstance(operand, ast.Constant) and isinstance(
                    operand.value, float
                ):
                    assert operand.value not in thresholds, (
                        f"compare.py:{node.lineno} compares against the literal "
                        f"{operand.value}, which is also a named constant. Use "
                        "the name."
                    )
