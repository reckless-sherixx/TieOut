# core/cli.py
import importlib

import typer

app = typer.Typer(help="tieout")


@app.callback()
def main() -> None:
    """tieout: multi-source reconciliation CLI."""


@app.command()
def version() -> None:
    """Print the tieout version."""
    from importlib.metadata import version as _v
    typer.echo(_v("tieout"))


def _register_lane_subapp(module_path: str, command_name: str) -> bool:
    """Register a lane-owned Typer sub-app, tolerating ONLY its absence.

    Each lane's worktree must be able to run its own command before the other
    lane has landed, so a genuinely missing sub-app module is not an error --
    the command is simply not offered.

    Everything else IS an error and is re-raised. A bare `except ImportError:
    pass` here would turn a typo inside core/generator/cli.py, a missing
    dependency, or a sub-app module that forgot to define `app`, into a silent
    "no such command" with no traceback -- in a file neither Lane A nor Lane B
    may edit, and which therefore neither could debug.

    The discriminator is `ModuleNotFoundError.name`: it equals `module_path`
    only when the sub-app module itself is what is missing. A failed import
    *inside* the sub-app names the module IT could not find, and a sub-app that
    exists but exports no `app` raises plain ImportError (or AttributeError),
    never ModuleNotFoundError. All of those propagate.

    Returns True if the sub-app was registered.
    """
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        if exc.name == module_path:
            return False                    # the lane has not landed yet
        raise                               # a real failure inside the sub-app

    app.add_typer(module.app, name=command_name)
    return True


_register_lane_subapp("core.generator.cli", "generate")   # Lane A owns this module
_register_lane_subapp("core.matcher.cli", "run")          # Lane B owns this module

if __name__ == "__main__":
    app()
