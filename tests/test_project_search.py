import contextlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from baton.errors import ProjectError
from baton.manager import Manager
from baton.search import query_terms, render_search
import test_project_manager as fixtures


SearchManager = Manager


class ProjectSearchTest(unittest.TestCase):
    setUp = fixtures.ProjectManagerTest.setUp
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    checkout = fixtures.ProjectManagerTest.checkout
    create = fixtures.ProjectManagerTest.create

    def setUp(self) -> None:
        fixtures.ProjectManagerTest.setUp(self)
        self.make_repo("alpha")
        self.create("alpha")
        self.project = fixtures.read_state(self.projects)["projects"]["p3"]
        self.directory = Path(self.project["artifacts_path"])

    def search(self, query: str, **kwargs: object) -> dict:
        with patch.dict(os.environ, self.env, clear=True), contextlib.chdir(self.root):
            return SearchManager().search_projects(query, **kwargs)

    def save(self, project: dict, *, archived: bool = False, index: str = "other") -> None:
        layout = self.projects / ".git" / "baton" / "v2"
        path = layout / ("archive" if archived else "projects") / f"{index}.json"
        path.write_text(json.dumps(project))

    def test_literals_phrases_and_across_files(self) -> None:
        (self.directory / "plan.md").write_text("Token [literal].\nRefresh\n rotation here.\n")
        (self.directory / "note.txt").write_text("Separate evidence")
        result = self.search('TOKEN "refresh rotation" separate [literal].')
        self.assertEqual(result["total_matches"], 1)
        evidence = result["results"][0]["matches"]
        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence[0]["line"], 1)
        self.assertIn("example", render_search(result))
        self.assertEqual(self.search("missing")["results"], [])
        self.assertEqual(query_terms('Token token " refresh  rotation "'), ["token", "refresh rotation"])
        for query in ('', '""', 'x "unfinished'):
            with self.assertRaises(ProjectError):
                self.search(query)
        with self.assertRaises(ProjectError):
            self.search("token", limit=0)

    def test_unicode_excerpt_and_nonregular_file(self) -> None:
        (self.directory / "unicode.md").write_text("Unrelated line\nStraße evidence\n")
        os.mkfifo(self.directory / "pipe.txt")
        result = self.search("strasse")
        self.assertEqual(result["results"][0]["matches"][0]["line"], 2)
        self.assertIn("Straße", result["results"][0]["matches"][0]["excerpt"])
        self.assertTrue(any("nonregular" in warning for warning in result["coverage_warnings"]))

    def test_metadata_retired_and_pending(self) -> None:
        project = self.project
        project["previous_names"] = ["original"]
        project["repos"][0]["members"] = [{"branch": "old-branch", "member": "retired-label", "retirement_state": "retired"}]
        project["pending_member"] = {"entry": {"branch": "future-branch"}}
        self.save(project, index="p3")
        self.assertEqual(self.search("original retired-label future-branch")["total_matches"], 1)

    def test_archive_overlap_and_state_and_shared_directory(self) -> None:
        (self.directory / "plan.md").write_text("private-evidence")
        self.save(self.project, archived=True)
        result = self.search("example")
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["results"][0]["state"], "archived")
        self.assertEqual(self.search("example", state="active")["total_matches"], 0)
        other = {**self.project, "uuid": "b" * 32}
        self.save(other, archived=True, index="second")
        self.assertEqual(self.search("private-evidence")["total_matches"], 0)
        result = self.search("example")
        self.assertEqual(result["total_matches"], 2)
        self.assertTrue(any("Shared artifact" in item for item in result["coverage_warnings"]))
        other["repos"] = ["invalid"]
        self.save(other, archived=True, index="second")
        self.assertTrue(any("malformed" in item for item in self.search("example")["coverage_warnings"]))

    def test_conflicting_identity_and_cli_outside_git(self) -> None:
        (self.directory / "plan.md").write_text("unattributed")
        other = self.artifacts / "conflict"
        other.mkdir()
        (other / "plan.md").write_text("unattributed")
        self.save({**self.project, "artifacts_path": str(other)}, archived=True)
        result = self.search("unattributed")
        self.assertEqual(result["results"], [])
        self.assertTrue(any("Conflicting" in item for item in result["coverage_warnings"]))
        cli = self.cli("search", "example", "--json", cwd=self.root)
        self.assertEqual(json.loads(cli.stdout)["total_matches"], 1)
        self.assertEqual(json.loads(self.cli("search", "missing", "--json", cwd=self.root).stdout)["results"], [])

    def test_safe_traversal_exclusions_and_coverage(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("unsafe-evidence")
        (self.directory / "link.md").symlink_to(outside)
        hidden = self.directory / ".snapshot"
        hidden.mkdir()
        (hidden / "note.md").write_text("hidden-evidence")
        (self.directory / "blackboard-lock.md").write_text("lock-evidence")
        coordination = self.directory / "coordination"
        coordination.mkdir()
        (coordination / "overview.md").write_text("overview-evidence")
        (coordination / "history.md").write_text("history-evidence")
        for term in ("unsafe-evidence", "hidden-evidence", "lock-evidence", "overview-evidence", "history-evidence"):
            self.assertEqual(self.search(term, categories=("ordinary",))["total_matches"], 0)
        self.assertEqual(self.search("overview-evidence", categories=("coordination_overview",))["total_matches"], 1)
        self.assertEqual(self.search("history-evidence", categories=("coordination_history",))["total_matches"], 1)
        self.assertEqual(self.search("overview-evidence history-evidence")["total_matches"], 1)
        self.assertNotIn("coordination history", self.search("example")["coverage"]["excluded"])
        other = self.root / "alias"
        other.symlink_to(self.directory, target_is_directory=True)
        self.project["artifacts_path"] = str(other)
        self.save(self.project, index="p3")
        self.assertTrue(any("unsafe" in item for item in self.search("example")["coverage_warnings"]))

    def test_malformed_required_active_metadata_does_not_abort_other_results(self) -> None:
        archived = {**self.project, "uuid": "b" * 32}
        self.save(archived, archived=True)
        for field, value in (("name", None), ("name", 42), ("name", ""), ("uuid", 42), ("uuid", "")):
            with self.subTest(field=field, value=value):
                malformed = {**self.project, field: value}
                self.save(malformed, index="p3")
                result = self.search("example")
                self.assertEqual([item["uuid"] for item in result["results"]], [archived["uuid"]])
                self.assertTrue(any("malformed" in warning for warning in result["coverage_warnings"]))
        malformed = dict(self.project)
        del malformed["name"]
        self.save(malformed, index="p3")
        self.assertEqual(self.search("example")["total_matches"], 1)

    def test_malformed_optional_metadata_preserves_search_results(self) -> None:
        self.project.update(owner="invalid", previous_names=[None, 42])
        self.save(self.project, index="p3")
        result = self.search("example")
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["results"][0]["owner"], {"provider": None, "available": False})
        self.assertEqual(result["results"][0]["previous_names"], [])
        self.assertIn("example", render_search(result))
        self.assertTrue(any("Malformed owner" in warning for warning in result["coverage_warnings"]))
        self.assertTrue(any("previous_names" in warning for warning in result["coverage_warnings"]))

    def test_unsafe_alias_does_not_hide_safe_project_evidence(self) -> None:
        (self.directory / "plan.md").write_text("legitimate-evidence")
        alias = self.root / "unsafe-alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        self.save({**self.project, "uuid": "b" * 32, "artifacts_path": str(alias)}, archived=True)
        result = self.search("legitimate-evidence")
        self.assertEqual([item["uuid"] for item in result["results"]], [self.project["uuid"]])
        self.assertTrue(any("unsafe" in warning for warning in result["coverage_warnings"]))

    def test_unicode_evidence_uses_casefold_for_detection_and_location(self) -> None:
        for text, query, line, excerpt in (("ı\nI", "i", 2, "I"),
                                            ("first\nStraße", "strasse", 2, "Straße"),
                                            ("x\nStraße  next\nline", '"strasse next line"', 2, "Straße")):
            with self.subTest(text=text):
                (self.directory / "plan.md").write_text(text)
                result = self.search(query)
                evidence = next(match for match in result["results"][0]["matches"] if match["kind"] == "artifact")
                self.assertEqual(evidence["line"], line)
                self.assertIn(excerpt, evidence["excerpt"])

    def test_text_output_escapes_terminal_controls_in_all_fields(self) -> None:
        (self.directory / "plan.md").write_text("evidence \x1b]52;c;payload\x07")
        self.project["previous_names"] = ["former\nname\x1b[31m"]
        self.save(self.project, index="p3")
        result = self.search("evidence")
        result["coverage_warnings"].append("warning\x1b[31m")
        output = render_search(result)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\x07", output)
        self.assertIn(r"\x1b", output)
        self.assertIn(r"former\nname", output)

    def test_evidence_limit_keeps_later_term_detection_and_source_counts(self) -> None:
        for index in range(30):
            (self.directory / f"note-{index:02d}.md").write_text("needle" + (" final-marker" if index == 29 else ""))
        result = self.search("needle final-marker")["results"][0]
        self.assertEqual(result["artifact_source_matches"], 30)
        self.assertEqual(len(result["matches"]), 12)

    def test_ranking_limit_bounded_and_missing_artifacts(self) -> None:
        (self.directory / "note.md").write_text("needle " * 10000)
        for index, name in enumerate(("needle", "other")):
            directory = self.artifacts / f"other-{index}"
            directory.mkdir()
            (directory / "plan.md").write_text("needle")
            self.save({**self.project, "uuid": str(index) * 32, "name": name, "artifacts_path": str(directory)}, archived=True, index=str(index))
        result = self.search("needle", limit=1)
        self.assertEqual(result["total_matches"], 3)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["results"][0]["name"], "needle")
        self.assertTrue(all(len(e["excerpt"]) <= 300 for p in self.search("needle")["results"] for e in p["matches"]))
        self.project["artifacts_path"] = str(self.root / "missing")
        self.save(self.project, index="p3")
        self.assertTrue(any("Missing" in item for item in self.search("example")["coverage_warnings"]))


if __name__ == "__main__":
    unittest.main()
