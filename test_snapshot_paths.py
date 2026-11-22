"""Exercise snapshot isolation through the CLI."""

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


TASKS = Path(__file__).with_name("tasks").resolve()


class SnapshotPathsTest(unittest.TestCase):
    def test_ledgers_with_the_same_stem_keep_distinct_boards(self):
        with TemporaryDirectory() as scratch:
            directory = Path(scratch).resolve()
            first = directory / "shared #1.sqlite3"
            second = directory / "shared #1.db"

            def tasks(ledger, *args):
                return subprocess.check_output(
                    [str(TASKS), "--db", str(ledger), *args], text=True, timeout=10,
                ).strip()

            tasks(first, "add", "Review the same sample", "--dri", "Avery", "--step", "First ledger.")
            first_board = Path(tasks(first, "board"))
            board_before = first_board.read_bytes()

            tasks(second, "add", "Review the same sample", "--dri", "Morgan", "--step", "Second ledger.")
            second_board = Path(tasks(second, "board"))
            self.assertNotEqual(first_board, second_board)
            self.assertEqual(first_board.read_bytes(), board_before)
            self.assertIn("First ledger.", first_board.read_text(encoding="utf-8"))
            self.assertIn("Second ledger.", second_board.read_text(encoding="utf-8"))
