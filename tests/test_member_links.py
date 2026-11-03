from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from baton.errors import ProjectError
from baton.manifest import link_name, managed_links


class MemberLinksTest(unittest.TestCase):
    def test_named_and_unnamed_entries_use_exact_canonical_links(self) -> None:
        repo = {"id": "alpha", "path": "/work/first", "first_member": "first",
                "members": [{"id": "alpha", "path": "/work/second", "member": "second"},
                            {"id": "alpha", "path": "/work/third", "member": "third"}]}
        project = {"repos": [repo, {"id": "beta", "path": "/work/beta"}]}
        self.assertEqual(link_name(repo), "alpha--first")
        self.assertEqual(managed_links(project), {
            "alpha--first": Path("/work/first"), "alpha--second": Path("/work/second"),
            "alpha--third": Path("/work/third"), "beta": Path("/work/beta")})
        repo["retirement_state"] = "retired"
        self.assertEqual(set(managed_links(project)), {"alpha--second", "alpha--third", "beta"})

    def test_colliding_repository_and_member_links_are_rejected(self) -> None:
        project = {"repos": [{"id": "alpha", "path": "/work/first", "first_member": "first"},
                             {"id": "alpha--first", "path": "/work/other"}]}
        with self.assertRaisesRegex(ProjectError, "collides"):
            managed_links(project)
