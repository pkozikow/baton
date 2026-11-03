from pathlib import Path
import subprocess
import tempfile
import unittest

from baton.artifacts import ArtifactsMixin
from baton.errors import ProjectError
from baton.gitutil import require_agent_write_path


class AgentWritePathsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.checkout = self.root / "checkout"
        subprocess.run(["git", "init", "--quiet", str(self.checkout)], check=True)

    def test_accepts_temporary_and_checkout_directories(self) -> None:
        for path in (self.root, self.root / "future/draft", self.checkout / "plans/draft.md"):
            with self.subTest(path=path):
                require_agent_write_path(path)

    def test_rejects_existing_and_missing_git_path_components(self) -> None:
        for path in (self.checkout / ".git", self.checkout / ".git/new/draft.md",
                     self.checkout / "future/.git/draft.md", self.checkout / "future/.GIT/draft.md"):
            with self.subTest(path=path), self.assertRaisesRegex(ProjectError, "outside Git metadata"):
                require_agent_write_path(path)

    def test_rejects_symlinks_into_metadata(self) -> None:
        alias = self.root / "alias"
        alias.symlink_to(self.checkout / ".git", target_is_directory=True)
        with self.assertRaisesRegex(ProjectError, "outside Git metadata"):
            require_agent_write_path(alias / "new/draft.md")

    def test_rejects_separate_git_directory_without_git_component(self) -> None:
        metadata = self.root / "metadata"
        checkout = self.root / "separate"
        subprocess.run(["git", "init", "--quiet", "--separate-git-dir", str(metadata), str(checkout)], check=True)
        with self.assertRaisesRegex(ProjectError, "outside Git metadata"):
            require_agent_write_path(metadata / "future/draft.md")
        require_agent_write_path(checkout / "draft.md")

    def test_artifact_validation_rejects_missing_git_directory(self) -> None:
        manager = object.__new__(ArtifactsMixin)
        directory = self.checkout / "future/.git/projects/example"
        with self.assertRaisesRegex(ProjectError, "outside Git metadata"):
            manager.artifacts_git(directory)
        self.assertFalse((self.checkout / "future").exists())
