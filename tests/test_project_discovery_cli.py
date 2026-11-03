import json
from pathlib import Path
import unittest

import test_project_manager as fixtures


class ProjectDiscoveryCLITest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    create = fixtures.ProjectManagerTest.create

    def test_search_empty_registry_from_non_git_directory_without_write_override(self) -> None:
        self.env.pop("BATON_ALLOW_UNTRUSTED_WRITES", None)
        result = self.cli("search", "missing", "--json", cwd=self.root)
        data = json.loads(result.stdout)
        self.assertEqual(data["results"], [])
        self.assertEqual(data["total_matches"], 0)
        self.assertFalse(data["truncated"])
        self.assertEqual(fixtures.read_state(self.projects)["projects"], {})

    def test_json_and_text_search_preserve_registry_and_artifacts(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        project = fixtures.read_state(self.projects)["projects"]["p3"]
        directory = Path(project["artifacts_path"])
        plan = directory / "plan.md"
        plan.write_text("Token refresh\nrotation work.\n")
        before = fixtures.state_file(self.projects).read_bytes(), plan.read_bytes()
        self.env.pop("BATON_ALLOW_UNTRUSTED_WRITES", None)
        result = self.cli("search", 'token "refresh rotation"', "--state", "active", "--limit", "1", "--json")
        data = json.loads(result.stdout)
        self.assertEqual(data["json_version"], 1)
        self.assertEqual(data["total_matches"], 1)
        self.assertEqual(data["results"][0]["selector"], project["uuid"])
        self.assertEqual(data["results"][0]["master_path"], str(plan))
        self.assertIn(str(plan), self.cli("search", "token").stdout)
        self.assertEqual(before, (fixtures.state_file(self.projects).read_bytes(), plan.read_bytes()))

    def test_invalid_queries_limits_and_state_fail_clearly(self) -> None:
        for arguments, diagnostic in (([""], "query"), (["\"unfinished"], "quote"),
                                      (["term", "--limit", "0"], "positive"),
                                      (["term", "--limit", "-1"], "positive"),
                                      (["term", "--state", "unknown"], "invalid choice")):
            with self.subTest(arguments=arguments):
                result = self.cli("search", *arguments, "--json", ok=False)
                self.assertIn(diagnostic, result.stderr.lower())
                self.assertEqual(result.stdout, "")

    def test_unreadable_text_warning_does_not_corrupt_json(self) -> None:
        self.make_repo("alpha")
        self.create("alpha")
        project = fixtures.read_state(self.projects)["projects"]["p3"]
        (Path(project["artifacts_path"]) / "broken.md").write_bytes(b"\xff\xfe")
        data = json.loads(self.cli("search", "example", "--json").stdout)
        self.assertEqual(data["total_matches"], 1)
        self.assertTrue(any("broken.md" in warning for warning in data["coverage_warnings"]))


if __name__ == "__main__":
    unittest.main()
