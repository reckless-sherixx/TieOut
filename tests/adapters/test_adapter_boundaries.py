# Boundary-proof scope (read before editing this file):
#
# `tests/test_boundaries.py` proves the matcher cannot see ground truth. This
# file proves the neighbouring claim that Workstream B rests on: the real-format
# fixtures and the adapters that read them are independent of
# `core/generator/`.
#
# Why it has to be asserted rather than believed. Every input this system has
# ingested until now came out of its own generator. The one honest way to
# validate a *real*-format reader is against files the generator did not make --
# and the cheapest way for that independence to evaporate is not malice, it is
# convenience: a helper imported from `core.generator.rng` to build a fixture in
# a test, a narration template reused "just for the expected value". Either
# would turn a fixture test into the generator agreeing with itself, and neither
# would look wrong in review.
#
# So: no module under `core/adapters/` and no test module under
# `tests/adapters/` may import `core.generator`, and neither may name a
# generator module in a string literal. Same AST technique and the same
# docstring exemption as `tests/test_boundaries.py`, and the same honest limits:
# a dynamic `importlib.import_module("core.generator.rng")` is an `ast.Call` and
# is invisible here, as is a module path assembled at runtime. Those belong to
# code review. This catches the accident, which is the thing that actually
# happens.
#
# `tests/test_boundaries.py` is deliberately untouched. It is frozen, it owns a
# different claim, and widening it would couple two proofs that should be able
# to fail separately.

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
ADAPTERS_DIR = REPO_ROOT / "core" / "adapters"
ADAPTER_TESTS_DIR = REPO_ROOT / "tests" / "adapters"
FIXTURES_DIR = REPO_ROOT / "fixtures" / "real-formats"

#: Substrings that must not appear in an import, in either direction.
FORBIDDEN = ("generator", "scorer", "truth")


def _imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # `node.module` is None for a bare relative import; the aliases
            # still have to be checked, because `from .. import generator`
            # binds the name with no module qualifier at all.
            found.append(node.module or "")
            found += [alias.name for alias in node.names]
    return found


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """id() of every Constant that IS a module/class/function docstring.

    Identity-based, not value-based: an ordinary string that happens to share a
    docstring's text is still scanned. Docstrings are exempt for the same
    reason as in `tests/test_boundaries.py` -- the most natural docstring for a
    module in this package is the one that says it must not touch the
    generator, and a check that reddens CI over its own documentation is a
    check somebody deletes.
    """
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def _string_literals(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    exempt = _docstring_node_ids(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in exempt
    ]


#: This file is excluded from its own scan. A checker that forbids the literal
#: "core.generator" necessarily contains it, so scanning itself would fail on
#: the words that make the check readable -- and the fix a future developer
#: would reach for is weakening the assertion, not renaming the constant. The
#: exclusion is by exact path, so every other module in the directory, present
#: and future, is still scanned.
SELF = pathlib.Path(__file__).resolve()


def _python_files(directory: pathlib.Path) -> list[pathlib.Path]:
    assert directory.is_dir(), f"{directory} does not exist"
    paths = [
        p
        for p in directory.rglob("*.py")
        if "__pycache__" not in p.parts and p.resolve() != SELF
    ]
    assert paths, f"no .py files under {directory}; the check would pass vacuously"
    return paths


def test_adapter_modules_do_not_import_the_generator():
    for path in _python_files(ADAPTERS_DIR):
        for module in _imports(path):
            for banned in FORBIDDEN:
                assert banned not in module, f"{path} imports '{module}' (contains '{banned}')"


def test_adapter_test_modules_do_not_import_the_generator():
    """The one that matters most. An adapter importing the generator would be
    obvious; a *test* doing it would look like a convenience and would quietly
    make the fixture suite a conversation between the generator and itself."""
    for path in _python_files(ADAPTER_TESTS_DIR):
        for module in _imports(path):
            for banned in FORBIDDEN:
                assert banned not in module, f"{path} imports '{module}' (contains '{banned}')"


def test_adapter_test_modules_do_not_name_a_generator_module_in_a_literal():
    """Catches the runtime back door an import check cannot see: a module path
    handed to `importlib`, or a fixture path pointed into the generator's
    committed output."""
    for path in _python_files(ADAPTER_TESTS_DIR):
        for literal in _string_literals(path):
            assert "core.generator" not in literal, f"{path} names a generator module"
            assert "core/generator" not in literal, f"{path} names a generator path"


def test_adapter_modules_do_not_name_a_generator_module_in_a_literal():
    for path in _python_files(ADAPTERS_DIR):
        for literal in _string_literals(path):
            assert "core.generator" not in literal, f"{path} names a generator module"
            assert "core/generator" not in literal, f"{path} names a generator path"


def test_adapter_tests_read_only_hand_written_fixtures():
    """No adapter test may reach into the generator's committed output.

    `fixtures/seed42-*` and `fixtures/tiny/` are the generator's own datasets.
    A real-format test that read one of them would be measuring the thing that
    Workstream B exists to avoid measuring, and the file names alone are enough
    to catch it.
    """
    for path in _python_files(ADAPTER_TESTS_DIR):
        for literal in _string_literals(path):
            assert "seed42" not in literal, f"{path} reads a generator dataset"
            assert "fixtures/tiny" not in literal, f"{path} reads fixtures/tiny"


def test_every_hand_written_fixture_declares_its_provenance():
    """A fixture is only evidence if a reader can tell where it came from.

    Each file must open with a `#` comment block that says it was written by
    hand and names the schema it was written from. This is the machine-checked
    half of the claim `VALIDATION.md` will make in prose.
    """
    assert FIXTURES_DIR.is_dir(), f"{FIXTURES_DIR} does not exist"
    # Every file, not just `*.csv`. MT940 is a tag-delimited message and its
    # fixtures are `.sta`; a glob that named one extension would let each new
    # format quietly opt out of declaring its own provenance.
    fixtures = sorted(path for path in FIXTURES_DIR.iterdir() if path.is_file())
    assert fixtures, "no fixtures found; the check would pass vacuously"
    for path in fixtures:
        # latin-1 reads any byte sequence, which is the point: one of these
        # fixtures is deliberately not UTF-8.
        header = path.read_bytes().decode("latin-1")
        comments = [
            line for line in header.splitlines() if line.startswith("#")
        ]
        assert comments, f"{path.name} has no provenance comment block"
        text = "\n".join(comments).upper()
        assert "HAND-WRITTEN" in text, f"{path.name} does not say it is hand-written"
        assert "CORE/GENERATOR" in text, (
            f"{path.name} does not state its independence from core/generator"
        )
        assert "SOURCE SCHEMA" in text or "SCHEMA" in text, (
            f"{path.name} does not name the schema it was written from"
        )


def test_the_frozen_boundary_test_is_untouched():
    """`tests/test_boundaries.py` owns a different proof and is not this file's
    to widen. If it ever needs to change, that is a deliberate act with its own
    commit -- not a side effect of adding a format.

    It has changed exactly once since this pin was written, and that was such an
    act: defect close-out parametrised the two truth bans over a
    `CREDIBILITY_PERIMETER` of `core/matcher` **and** `core/llm`, because an
    accepted hypothesis becomes a `MatchGroup` the scorer grades like any other.
    The names below were updated with it. That is the intended workflow -- the
    pin exists to make widening deliberate, not to make it impossible.

    What it still guards is unchanged: both proofs must exist under some name,
    and neither may grow an adapters concern.
    """
    frozen = REPO_ROOT / "tests" / "test_boundaries.py"
    assert frozen.is_file()
    source = frozen.read_text(encoding="utf-8")
    assert "def test_perimeter_cannot_see_ground_truth(" in source
    assert "def test_perimeter_does_not_open_truth_files(" in source
    assert "def test_core_has_no_web_dependency():" in source
    # The perimeter itself is pinned, not just the function names: dropping
    # core/llm back out of it would otherwise leave every assertion above
    # green while the ban stopped covering the package it was widened for.
    assert '"core/matcher"' in source and '"core/llm"' in source, (
        "tests/test_boundaries.py no longer names both perimeter packages"
    )
    assert "adapters" not in source, (
        "tests/test_boundaries.py has grown an adapters concern; adapter "
        "boundary proofs belong in this file so the two can fail separately"
    )
