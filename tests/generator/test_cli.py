"""Task A.5 -- the `recon generate` sub-app.

`core/cli.py` is frozen and already registers `core.generator.cli:app`, so these
tests drive the **root** app the way a user does. That also covers the wiring:
a sub-app that imports cleanly but exports no `app`, or one whose callback needs
a subcommand name, would show up here as a missing command rather than as an
import error.
"""

import json

import pytest
from typer.testing import CliRunner

from core.cli import app
from core.generator.cli import DEFAULT_COUNT, DEFAULT_SEED, default_out, parse_defect_mix
from core.generator.defects import DEFECT_REGISTRY

FILES = ("orders.csv", "psp.csv", "bank.csv", "truth.json")


def test_generate_writes_four_files(tmp_path):
    result = CliRunner().invoke(
        app, ["generate", "--seed", "42", "--count", "50", "--out", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    for name in FILES:
        assert (tmp_path / name).exists()


def test_the_command_is_registered_on_the_root_app():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "generate" in result.output


def test_two_identical_invocations_are_byte_identical(tmp_path):
    runner = CliRunner()
    a, b = tmp_path / "a", tmp_path / "b"
    for out in (a, b):
        result = runner.invoke(
            app, ["generate", "--seed", "42", "--count", "60", "--out", str(out)]
        )
        assert result.exit_code == 0, result.output
    for name in FILES:
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_the_seed_is_recorded_in_the_truth_file(tmp_path):
    result = CliRunner().invoke(
        app, ["generate", "--seed", "7", "--count", "50", "--out", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    truth = json.loads((tmp_path / "truth.json").read_text(encoding="utf-8"))
    assert truth["seed"] == 7
    assert truth["record_count"] == 50


def test_the_defaults_are_the_seeded_adversarial_dataset():
    """`--seed 42 --count 500` is the definition of done; it is also the default."""
    assert (DEFAULT_SEED, DEFAULT_COUNT) == (42, 500)
    assert default_out(42, 500).as_posix() == "fixtures/seed42-500"


def test_a_defect_mix_override_reaches_the_generator(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "generate",
            "--seed",
            "42",
            "--count",
            "100",
            "--out",
            str(tmp_path),
            "--defect-mix",
            "garbled_narration=6",
        ],
    )
    assert result.exit_code == 0, result.output
    truth = json.loads((tmp_path / "truth.json").read_text(encoding="utf-8"))
    garbled = [
        d for d in truth["injected_defects"] if d["defect_type"] == "garbled_narration"
    ]
    assert len(garbled) == 6


def test_an_unknown_defect_name_fails_loudly(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "generate",
            "--count",
            "50",
            "--out",
            str(tmp_path),
            "--defect-mix",
            "not_a_defect=1",
        ],
    )
    assert result.exit_code != 0
    assert "not_a_defect" in result.output
    assert not (tmp_path / "truth.json").exists(), "a rejected run must write nothing"


def test_defect_mix_accepts_pairs_and_json():
    assert parse_defect_mix("rounding_break=3") == {"rounding_break": 3}
    assert parse_defect_mix(" rounding_break = 3 , split_settlement=2 ") == {
        "rounding_break": 3,
        "split_settlement": 2,
    }
    assert parse_defect_mix('{"rounding_break": 3}') == {"rounding_break": 3}
    assert parse_defect_mix(None) is None
    assert parse_defect_mix("") is None


@pytest.mark.parametrize("bad", ["rounding_break", "rounding_break=x", "=3", "[1,2]"])
def test_a_malformed_defect_mix_is_rejected(bad):
    with pytest.raises(ValueError):
        parse_defect_mix(bad)


def test_a_zero_count_is_rejected(tmp_path):
    result = CliRunner().invoke(
        app, ["generate", "--count", "0", "--out", str(tmp_path)]
    )
    assert result.exit_code != 0


def test_the_run_reports_what_it_wrote(tmp_path):
    result = CliRunner().invoke(
        app, ["generate", "--seed", "42", "--count", "50", "--out", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "50" in result.output
    # Derived from the registry rather than spelled: this test's job is that the
    # run reports the class count, not that the count is any particular number.
    assert str(len(DEFECT_REGISTRY)) in result.output, (
        "every defect class should be reported"
    )


# --- --export-as / --dirty --------------------------------------------------


EXPORT_FILES = ("razorpay_settlement.csv", "hdfc_statement.csv", "shopify_orders.csv")


def test_export_as_razorpay_writes_the_three_real_format_files(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "generate", "--seed", "42", "--count", "50",
            "--out", str(tmp_path), "--export-as", "razorpay",
        ],
    )
    assert result.exit_code == 0, result.output
    for name in (*FILES, *EXPORT_FILES):
        assert (tmp_path / name).exists(), name


def test_export_as_leaves_the_canonical_files_alone(tmp_path):
    runner = CliRunner()
    plain, exported = tmp_path / "plain", tmp_path / "exported"
    assert runner.invoke(
        app, ["generate", "--seed", "42", "--count", "50", "--out", str(plain)]
    ).exit_code == 0
    assert runner.invoke(
        app,
        [
            "generate", "--seed", "42", "--count", "50",
            "--out", str(exported), "--export-as", "razorpay",
        ],
    ).exit_code == 0
    for name in FILES:
        assert (plain / name).read_bytes() == (exported / name).read_bytes(), name


def test_an_unknown_export_format_is_refused_by_name(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "generate", "--seed", "42", "--count", "50",
            "--out", str(tmp_path), "--export-as", "stripe",
        ],
    )
    assert result.exit_code == 2
    assert "not a known export format" in result.output


def test_dirty_without_export_as_has_nothing_to_damage(tmp_path):
    result = CliRunner().invoke(
        app,
        ["generate", "--seed", "42", "--count", "50", "--out", str(tmp_path), "--dirty"],
    )
    assert result.exit_code == 2
    assert "needs --export-as" in result.output


def test_dirty_damages_only_the_exported_files(tmp_path):
    runner = CliRunner()
    clean, dirty = tmp_path / "clean", tmp_path / "dirty"
    base = ["generate", "--seed", "42", "--count", "50", "--export-as", "razorpay"]
    assert runner.invoke(app, [*base, "--out", str(clean)]).exit_code == 0
    assert runner.invoke(app, [*base, "--out", str(dirty), "--dirty"]).exit_code == 0

    for name in FILES:
        assert (clean / name).read_bytes() == (dirty / name).read_bytes(), name
    for name in EXPORT_FILES:
        assert (clean / name).read_bytes() != (dirty / name).read_bytes(), name


def test_a_dirty_export_is_deterministic_too(tmp_path):
    runner = CliRunner()
    a, b = tmp_path / "a", tmp_path / "b"
    base = [
        "generate", "--seed", "42", "--count", "50",
        "--export-as", "razorpay", "--dirty",
    ]
    for out in (a, b):
        assert runner.invoke(app, [*base, "--out", str(out)]).exit_code == 0
    for name in (*FILES, *EXPORT_FILES):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name
