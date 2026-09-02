"""core/cli.py registers each lane's sub-app optionally, but only its ABSENCE
is tolerated. A real failure inside a sub-app must surface as a traceback, not
as a missing command -- neither Lane A nor Lane B may edit core/cli.py, so a
swallowed error there is a bug they cannot diagnose.
"""

import sys
import types

import pytest

from core.cli import _register_lane_subapp


def test_a_missing_subapp_module_is_tolerated():
    """The lane has not landed yet: no command, no error."""
    assert _register_lane_subapp("core.generator.definitely_not_here", "nope") is False


def test_a_missing_import_inside_the_subapp_is_re_raised(monkeypatch):
    """A typo inside core/generator/cli.py must not read as "no such command"."""
    broken = types.ModuleType("core.generator.cli")
    broken.__spec__ = None

    def explode(name):
        assert name == "core.generator.cli"
        raise ModuleNotFoundError(
            "No module named 'panda'", name="panda"
        )

    monkeypatch.setattr("core.cli.importlib.import_module", explode)

    with pytest.raises(ModuleNotFoundError) as exc:
        _register_lane_subapp("core.generator.cli", "generate")
    assert exc.value.name == "panda"


def test_a_subapp_without_an_app_attribute_is_re_raised(monkeypatch):
    """A sub-app module that exists but exports no `app` is a real bug."""
    empty = types.ModuleType("core.matcher.cli")
    monkeypatch.setitem(sys.modules, "core.matcher.cli", empty)

    with pytest.raises(AttributeError):
        _register_lane_subapp("core.matcher.cli", "run")


def test_the_root_cli_exposes_version_without_any_lane_present():
    """`recon version` works on a bare Phase 0 tree."""
    from typer.testing import CliRunner

    from core.cli import app

    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip()
