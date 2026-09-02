# Boundary-proof scope (read before editing this file):
#
# What this file proves: no package inside the CREDIBILITY_PERIMETER below
# does, in its source text, import generator/scorer/truth modules or
# reference the literal string "truth" in any string literal (including
# f-string parts), and nothing under core/ imports a web-framework
# dependency. It is a good-faith structural check against accidental and
# lazy coupling between the generator/matcher/scorer lanes -- the kind of
# thing that creeps in from a careless import or a debug call to a truth
# fixture, not a defence against a determined adversary who wants to cheat.
#
# The perimeter is core/matcher AND core/llm. core/llm was outside it until
# defect close-out, on the reading that the LLM only ever "proposes" -- but
# an accepted hypothesis becomes a MatchGroup that the scorer grades exactly
# like a deterministic one (core/llm/pipeline.py: `merge`, tier "LLM"), so
# the LLM package is inside the credibility perimeter in substance and was
# outside it only in the test. A prompt built from truth.json, or an analyst
# that read the labels file to decide what to propose, would move
# `recall`, `false_match_rate` and `trap_capture_rate` with nothing in this
# file going red. That is the same class of mistake the matcher ban exists
# to catch, reached one module later.
#
# Both bans apply to the whole perimeter uniformly. There is deliberately
# no per-directory exemption list: the moment one directory is allowed a
# carve-out, the argument for the ban stops being structural.
#
# Module/class/function docstrings are exempt from the "truth" string-literal
# scan (see _docstring_node_ids below). A docstring cannot perform I/O --
# open(__doc__) is not a plausible cheat, and anything that actually opened a
# file would need a call expression this check would see elsewhere. Against
# that near-zero risk, the false-positive cost is concrete and likely: the
# most natural docstring for a module in core/matcher/ is exactly the one
# that would trip a value-blind check -- something like """Tier matching.
# This module must never read truth.json.""" A developer who writes that,
# watches CI go red, and is told the boundary test objects to their own
# documentation will be tempted to weaken the assertion rather than reword
# it. That is how the proof dies. Exempting docstrings costs nothing real
# and removes the most likely path to the test being disabled. Every other
# string literal -- including one that merely happens to share a docstring's
# exact text -- is still scanned; the exemption is scoped to the specific
# docstring Constant node by identity, not by value.
#
# What it deliberately does NOT (and structurally cannot) catch:
#   - Dynamic imports: importlib.import_module("core.generator.rng") or
#     __import__("core.generator") are ast.Call nodes, not ast.Import /
#     ast.ImportFrom, so _imports() never sees them.
#   - A truth-file path assembled at runtime with no literal "truth"
#     substring in source, e.g. Path("fixtures") / f"{name}.json" where
#     name comes from config/env -- there is no string literal to inspect.
#   - Comments are intentionally out of scope: the check walks the AST, and
#     comments never become AST nodes.
#
# If any gap above is ever exploited, the fix belongs in code review and
# process, not in a more elaborate static check that will always have its
# own gap one level further out.

import ast
import pathlib

import pytest

FORBIDDEN_IN_PERIMETER = ("generator", "scorer", "truth")

#: Kept under the old name so nothing outside this file breaks on the rename.
FORBIDDEN_FOR_MATCHER = FORBIDDEN_IN_PERIMETER

#: Every package whose output the scorer grades, and which therefore must not
#: be able to see what it is graded against. `core/matcher` is the obvious
#: one; `core/llm` is here because an accepted hypothesis becomes a
#: `MatchGroup` on the same footing as a tier's -- see the header.
#:
#: Relative POSIX paths, resolved against REPO_ROOT, so the parametrised test
#: ids read as the directories they guard.
CREDIBILITY_PERIMETER = ("core/matcher", "core/llm")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _perimeter_paths(package: str) -> list[pathlib.Path]:
    """Every .py file under one perimeter package.

    Asserts the directory exists and is non-empty at the point of use rather
    than returning an empty list: a perimeter package that has been renamed or
    moved must fail loudly here, because the alternative is a ban that passes
    over nothing at all and reports green.
    """
    directory = REPO_ROOT / package
    assert directory.is_dir(), f"{directory} does not exist"
    paths = list(directory.rglob("*.py"))
    assert paths, f"no .py files found under {directory}; test would pass vacuously"
    return paths


def _imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            # node.module is None for bare relative imports (`from . import x`,
            # `from .. import x`), which previously made the banned-substring
            # check compare against "" and silently pass. The names being
            # imported (node.names) must be checked too: `from .. import
            # generator` binds "generator" via an alias with no module
            # qualifier at all.
            found.append(node.module or "")
            found += [alias.name for alias in node.names]
    return found


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """id() of every ast.Constant node that IS a module/class/function
    docstring: the first statement of the body, when that statement is an
    ast.Expr wrapping a string ast.Constant. Identity-based (id()), not
    value-based, because an ordinary non-docstring string literal may
    legitimately share the same text as some docstring elsewhere in the
    file -- that literal must still be scanned."""
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
    """All string constant values in the file's AST, including f-string
    parts (which appear as ast.Constant nodes nested inside ast.JoinedStr),
    EXCLUDING module/class/function docstrings (see the module comment
    above for why). Comments are never AST nodes, so a code comment can
    never trip this check either."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstring_ids = _docstring_node_ids(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_ids:
            found.append(node.value)
    return found


@pytest.mark.parametrize("package", CREDIBILITY_PERIMETER)
def test_perimeter_cannot_see_ground_truth(package):
    """No graded package imports the generator, the scorer or a truth module.

    Parametrised over the whole perimeter rather than asserted for
    `core/matcher` alone: `core/llm` produces `MatchGroup`s the scorer grades
    on the same footing, so a truth import there is worth exactly as much to
    the metrics as one in a tier.
    """
    for path in _perimeter_paths(package):
        for mod in _imports(path):
            for banned in FORBIDDEN_IN_PERIMETER:
                assert banned not in mod, f"{path} imports '{mod}' (contains '{banned}')"


@pytest.mark.parametrize("package", CREDIBILITY_PERIMETER)
def test_perimeter_does_not_open_truth_files(package):
    """No graded package names a truth file in a non-docstring string literal.

    For `core/llm` this is the ban that matters most, and it is narrower than
    it looks: the analyst's prompt is assembled from string literals in
    `core/llm/prompts.py`, so a fixture path, a labels filename or a
    "these subjects are unresolvable" hint spliced into the prompt is caught
    here as a string rather than as an import. Module and function docstrings
    stay exempt for the reason the header gives -- `prompts.py`'s own
    docstring says at length that the prompt must never contain ground truth,
    which is precisely the sentence a value-blind check would trip on.
    """
    for path in _perimeter_paths(package):
        for literal in _string_literals(path):
            assert "truth" not in literal.lower(), \
                f"{path} references truth data"


def test_core_has_no_web_dependency():
    core_dir = REPO_ROOT / "core"
    assert core_dir.is_dir(), f"{core_dir} does not exist"
    paths = list(core_dir.rglob("*.py"))
    assert paths, f"no .py files found under {core_dir}; test would pass vacuously"
    for path in paths:
        for mod in _imports(path):
            assert not mod.startswith(("fastapi", "uvicorn", "starlette")), \
                f"{path} imports web dependency '{mod}'"
