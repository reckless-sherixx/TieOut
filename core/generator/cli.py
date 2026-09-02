"""`recon generate` (Task A.5).

`core/cli.py` is frozen and already does `app.add_typer(core.generator.cli.app,
name="generate")`. All this module owes it is a module-level `app`.

The options live on the sub-app's **callback**, with `invoke_without_command=True`,
so the command reads `recon generate --seed 42 --count 500` rather than
`recon generate run --seed 42 ...`. A sub-app whose options hang off a nested
command would still pass an import check and still fail the definition of done.

Defaults are `--seed 42 --count 500` on purpose: the definition of done is a
bare `recon generate --seed 42 --count 500`, and a default that disagreed with
it would give the demo and the docs two different datasets.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from .defects import DEFECT_REGISTRY
from .emit import emit_dataset
from .export import EXPORT_FORMATS, ExportError, dirty_export, export_dataset
from .pipeline import build_dataset

DEFAULT_SEED = 42
DEFAULT_COUNT = 500

app = typer.Typer(help="Generate a seeded, fully labelled adversarial dataset.")


def default_out(seed: int, count: int) -> Path:
    """Where a run lands when `--out` is omitted. Lane A owns `fixtures/seed42-*/`."""
    return Path("fixtures") / f"seed{seed}-{count}"


def parse_defect_mix(text: str | None) -> dict[str, int] | None:
    """Parse `--defect-mix`, as either `name=count,name=count` or a JSON object.

    Returns `None` for an omitted or empty value, which means "use
    `DEFAULT_DEFECT_MIX`" -- the single copy of those proportions. An override
    replaces only the entries it names.
    """
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None

    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--defect-mix is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("--defect-mix JSON must be an object of name -> count")
        pairs = list(parsed.items())
    else:
        pairs = []
        for chunk in text.split(","):
            name, separator, count = chunk.partition("=")
            if not separator:
                raise ValueError(
                    f"--defect-mix entry {chunk.strip()!r} is missing '=': "
                    "write it as name=count, or pass a JSON object"
                )
            pairs.append((name, count))

    mix: dict[str, int] = {}
    for name, count in pairs:
        name = str(name).strip()
        if not name:
            raise ValueError("--defect-mix has an entry with no defect name")
        try:
            mix[name] = int(str(count).strip())
        except ValueError as exc:
            raise ValueError(f"--defect-mix {name}: {count!r} is not an integer") from exc
    return mix


@app.callback(invoke_without_command=True)
def generate(
    ctx: typer.Context,
    seed: Annotated[
        int, typer.Option("--seed", help="Seed. The same seed emits byte-identical CSVs.")
    ] = DEFAULT_SEED,
    count: Annotated[
        int, typer.Option("--count", min=1, help="Rows of orders.csv to emit.")
    ] = DEFAULT_COUNT,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Output directory. Default: fixtures/seed<seed>-<count>."),
    ] = None,
    defect_mix: Annotated[
        str | None,
        typer.Option(
            "--defect-mix",
            help="Override defect counts: 'name=count,name=count' or a JSON object. "
            "Omitted means the default mix.",
        ),
    ] = None,
    export_as: Annotated[
        str | None,
        typer.Option(
            "--export-as",
            help="Also write the dataset in the real merchant file formats: "
            "'razorpay' emits a settlement report, an HDFC statement and a "
            "Shopify order export alongside the canonical CSVs.",
        ),
    ] = None,
    dirty: Annotated[
        bool,
        typer.Option(
            "--dirty",
            help="With --export-as, inject file-level damage into the EXPORTED "
            "files: a sub-paise decimal, a line cut short, a BOM and a latin-1 "
            "narration. Every injection is a row the dataset does not contain, "
            "so quarantine must isolate them and the metrics must not move.",
        ),
    ] = False,
) -> None:
    """Emit orders.csv, psp.csv, bank.csv and truth.json into --out.

    `--export-as` ADDS files. It never changes the canonical four, so
    `recon generate --seed 42 --count 500` and the same command with
    `--export-as razorpay` produce byte-identical `orders.csv`, `psp.csv`,
    `bank.csv`, `psp_gst_invoice.csv` and `truth.json`. That is what makes the
    round trip a measurement rather than a restatement: both paths read one
    dataset and one ground truth.
    """
    if ctx.invoked_subcommand is not None:
        return

    try:
        if dirty and export_as is None:
            raise ValueError(
                "--dirty damages the EXPORTED files, so it needs --export-as; "
                "on its own there is nothing for it to damage"
            )
        if export_as is not None and export_as not in EXPORT_FORMATS:
            raise ValueError(
                f"--export-as {export_as!r} is not a known export format; "
                f"known formats: {', '.join(EXPORT_FORMATS)}"
            )
        mix = parse_defect_mix(defect_mix)
        if mix is not None:
            unknown = sorted(set(mix) - set(DEFECT_REGISTRY))
            if unknown:
                raise ValueError(
                    f"unknown defect type(s): {', '.join(unknown)}. "
                    f"Known types: {', '.join(sorted(DEFECT_REGISTRY))}"
                )
        batches, injections = build_dataset(seed, count, mix)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    out_dir = out if out is not None else default_out(seed, count)
    emit_dataset(batches, injections, out_dir=out_dir, seed=seed)

    if export_as is not None:
        try:
            exported = export_dataset(batches, out_dir)
        except ExportError as exc:
            typer.echo(f"error: export failed: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(
            f"exported {len(exported)} real-format file(s) as {export_as}: "
            f"{', '.join(path.name for path in exported)}"
        )
        if dirty:
            injected = dirty_export(out_dir)
            typer.echo(
                f"injected {len(injected)} file-level defect(s) into the export; "
                f"every one is a row the dataset does not contain, so quarantine "
                f"must isolate them and no metric may move"
            )

    classes = {result.defect_type for result in injections}
    settlements = len(batches)
    lines = sum(len(batch.all_bank_lines) for batch in batches)
    unresolvable = sum(1 for result in injections if not result.resolvable)
    typer.echo(
        f"wrote {out_dir} -- {count} orders, {settlements} settlements, "
        f"{lines} bank lines"
    )
    typer.echo(
        f"injected {len(injections)} defects across {len(classes)} of "
        f"{len(DEFECT_REGISTRY)} classes ({unresolvable} unresolvable, by design)"
    )
    missing = sorted(set(DEFECT_REGISTRY) - classes)
    if missing:
        typer.echo(f"warning: no instance of {', '.join(missing)}", err=True)
