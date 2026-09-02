from datetime import date

from core.connectors.watched_folder import WatchedFolderConnector


def test_it_is_unavailable_when_no_directory_is_configured():
    assert WatchedFolderConnector(None).available() is False


def test_it_reads_every_file_it_finds(tmp_path):
    (tmp_path / "hdfc-aug.csv").write_bytes(b"Date,Narration\n")
    (tmp_path / "icici-aug.csv").write_bytes(b"Tran Date,Remarks\n")
    files = WatchedFolderConnector(tmp_path).fetch(date(2026, 8, 1), date(2026, 8, 31))
    assert sorted(f.suggested_name for f in files) == ["hdfc-aug.csv", "icici-aug.csv"]


def test_it_skips_a_dotfile_rather_than_quarantining_it(tmp_path):
    """`.DS_Store` and a half-written `.tmp` are not merchant files, and
    sending them to quarantine would train a merchant to ignore quarantine."""
    (tmp_path / ".DS_Store").write_bytes(b"\x00")
    (tmp_path / "real.csv").write_bytes(b"Date,Narration\n")
    files = WatchedFolderConnector(tmp_path).fetch(date(2026, 8, 1), date(2026, 8, 31))
    assert [f.suggested_name for f in files] == ["real.csv"]


def test_a_missing_directory_is_an_empty_fetch_not_a_crash(tmp_path):
    assert WatchedFolderConnector(tmp_path / "nope").fetch(
        date(2026, 8, 1), date(2026, 8, 31)
    ) == []


def test_a_subdirectory_is_not_a_statement(tmp_path):
    """Only files. A directory has no bytes to hand to an adapter, and
    descending into one would make the watched folder's contents unbounded."""
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "old.csv").write_bytes(b"Date,Narration\n")
    (tmp_path / "real.csv").write_bytes(b"Date,Narration\n")
    files = WatchedFolderConnector(tmp_path).fetch(date(2026, 8, 1), date(2026, 8, 31))
    assert [f.suggested_name for f in files] == ["real.csv"]
