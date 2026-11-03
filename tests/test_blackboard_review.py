import unittest

from blackboard import BlackboardError
from test_project_manager import artifact_path
import test_project_blackboard as fixtures
import test_sqlite_blackboard as storage


class BlackboardReviewTest(unittest.TestCase):
    setUp = fixtures.ProjectBlackboardTest.setUp
    run_command = fixtures.ProjectBlackboardTest.run_command
    git = fixtures.ProjectBlackboardTest.git
    cli = fixtures.ProjectBlackboardTest.cli
    make_repo = fixtures.ProjectBlackboardTest.make_repo
    checkout = fixtures.ProjectBlackboardTest.checkout
    create = fixtures.ProjectBlackboardTest.create
    replace_interactively = fixtures.ProjectBlackboardTest.replace_interactively
    start_project = fixtures.ProjectBlackboardTest.start_project
    board_dir = fixtures.ProjectBlackboardTest.board_dir
    snapshot = fixtures.ProjectBlackboardTest.snapshot
    post = fixtures.ProjectBlackboardTest.post
    generation = fixtures.ProjectBlackboardTest.generation

    def test_interrupted_journal_record_recovers_without_replacing_lock(self) -> None:
        self.start_project()
        self.post("Keep the note through rename")
        lock = self.board_dir() / "blackboard-lock.md"
        inode = lock.stat().st_ino
        with lock.open("ab") as handle:
            handle.write(b'{"status": "renaming", "new_project":')
        self.cli("rename", "renamed", "p3")
        renamed = artifact_path(self.projects, self.artifacts, "renamed") / "blackboard-lock.md"
        self.assertEqual(renamed.stat().st_ino, inode)
        self.assertTrue(renamed.read_bytes().endswith(b"\n"))
        self.assertIn("Keep the note through rename", self.cli("blackboard", "read", "--selector", "renamed").stdout)

    def test_same_name_replacement_uses_an_independent_directory(self) -> None:
        self.start_project()
        self.post("Preserve the first project")
        previous = self.snapshot()
        original = self.board_dir()
        inode = (original / "blackboard-lock.md").stat().st_ino
        self.replace_interactively(b"y\n", name="example")
        self.assertNotEqual(self.snapshot()["generation"], previous["generation"])
        self.assertNotEqual(self.board_dir(), original)
        self.assertEqual((original / "blackboard-lock.md").stat().st_ino, inode)
        self.assertIn("Preserve the first project", (original / "coordination" / "history.md").read_text())

    def test_reused_project_can_be_renamed_and_cleaned_keeping_both_archives(self) -> None:
        self.start_project()
        self.post("First project's preserved board")
        first = self.board_dir()
        self.cli("cleanup", "p3")
        self.create("alpha")
        second = self.board_dir()
        self.post("Second generation after rename")
        self.cli("rename", "renamed", "p3")
        self.cli("cleanup", "p3")
        self.assertIn("First project's preserved board", (first / "coordination" / "history.md").read_text())
        self.assertIn("Second generation after rename", (second / "coordination" / "history.md").read_text())
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")


class BlackboardJournalTest(unittest.TestCase):
    setUp = storage.SQLiteStorageTest.setUp

    def test_completed_corrupt_journal_record_does_not_reactivate_board(self) -> None:
        original = self.board.path.read_bytes()
        lock = self.root / "blackboard-lock.md"
        journal = lock.read_bytes()
        inode = lock.stat().st_ino
        for record in (b'{"status": "finalized", BROKEN}\n',
                       b'{"status": "finalized"}\n',
                       b'{"project": "example", "generation": "other", "status": "unknown"}\n',
                       b'{"project": "example", "generation": "other", "status": "finalized"}\n'):
            with self.subTest(record=record):
                lock.write_bytes(journal + record)
                with self.assertRaises(BlackboardError):
                    self.board.post("worker", "Must not resume the older active record")
                self.assertEqual(self.board.path.read_bytes(), original)
                self.assertEqual(lock.read_bytes(), journal + record)
                self.assertEqual(lock.stat().st_ino, inode)
