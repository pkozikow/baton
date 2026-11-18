import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock

from baton.errors import ProjectError
from baton.gdoc import adopt_text, context, gws, header, image_size, pull_text, read_structure, revision_rejected

import test_project_manager as fixtures
from test_project_manager import artifact_path


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gdoc"
DOC = "doc-alpha-000000000000000000"
OTHER = "doc-beta-0000000000000000000"
FIRST = "flowchart LR\n  a --> b\n"
SECOND = "sequenceDiagram\n  a ->> b: hi\n"
THIRD = "graph TD\n  x --> y\n"


def digest(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()


def block(source: str, mark: str | None = None) -> str:
    return (f"<!-- baton-gdoc {mark} -->\n" if mark else "") + "```mermaid\n" + source + "```\n"


def image_mark(image: str, source: str | None) -> str:
    return f"image={image}" + (f" sha256={digest(source)}" if source is not None else "")


def image(object_id: str) -> tuple[str, str]:
    return ("image", object_id)


class P:
    """A Doc paragraph made of text runs and inline images."""
    def __init__(self, *parts: Any, bullet: bool = False, style: str = "NORMAL_TEXT") -> None:
        self.parts, self.bullet, self.style = parts, bullet, style


class Table:
    def __init__(self, *rows: list[P]) -> None:
        self.rows = rows


def document(*items: Any, revision: str = "r1", tabs: int = 1) -> dict[str, Any]:
    index = 1

    def paragraph(spec: P) -> dict[str, Any]:
        nonlocal index
        start, elements = index, []
        for part in (*spec.parts, "\n"):
            if isinstance(part, tuple):
                elements.append({"startIndex": index, "endIndex": index + 1,
                                 "inlineObjectElement": {"inlineObjectId": part[1]}})
                index += 1
            else:
                size = len(part.encode("utf-16-le")) // 2
                elements.append({"startIndex": index, "endIndex": index + size, "textRun": {"content": part}})
                index += size
        result: dict[str, Any] = {"elements": elements, "paragraphStyle": {"namedStyleType": spec.style}}
        if spec.bullet:
            result["bullet"] = {"listId": "list"}
        return {"startIndex": start, "endIndex": index, "paragraph": result}

    content: list[dict[str, Any]] = [{"endIndex": 1, "sectionBreak": {}}]
    for item in items:
        spec = item if isinstance(item, (P, Table)) else P(item)
        if isinstance(spec, Table):
            start = index
            index += 1
            rows = []
            for row in spec.rows:
                index += 1
                cells = []
                for cell in row:
                    index += 1
                    cells.append({"content": [paragraph(cell)]})
                rows.append({"tableCells": cells})
            content.append({"startIndex": start, "endIndex": index, "table": {"tableRows": rows}})
        else:
            content.append(paragraph(spec))
    tab = {"tabProperties": {"tabId": "t.0"}, "documentTab": {"body": {"content": content}}}
    return {"documentId": DOC, "revisionId": revision, "tabs": [tab] + [
        {"tabProperties": {"tabId": f"t.{n}"}, "documentTab": {"body": {"content": []}}} for n in range(1, tabs)]}


def doc_id_in(text: str) -> str:
    return header(DOC) + "\n\n" + text


class TransformTest(unittest.TestCase):
    def pull(self, current: str, export: str, doc: dict[str, Any]) -> tuple[str, list[str]]:
        return pull_text(current, export, read_structure(doc), DOC)

    def refused(self, current: str, export: str, doc: dict[str, Any], *expected: str) -> str:
        with self.assertRaises(ProjectError) as caught:
            self.pull(current, export, doc)
        for text in expected:
            self.assertIn(text, str(caught.exception))
        return str(caught.exception)

    def test_marked_image_becomes_its_block(self) -> None:
        current = doc_id_in("old\n\n" + block(FIRST, image_mark("kix.a", FIRST)))
        export = "# Title\n\n![][image1]\n\nAfter.\n\n[image1]: <data:image/png;base64,AAAA>\n"
        new, notes = self.pull(current, export, document(P("Title", style="HEADING_1"), P(image("kix.a")), "After."))
        self.assertEqual(new, doc_id_in("# Title\n\n" + block(FIRST, image_mark("kix.a", FIRST)) + "\nAfter.\n"))
        self.assertNotIn("base64", new)
        self.assertEqual(notes, [])

    def test_pending_placeholder_paragraph_becomes_its_block(self) -> None:
        current = block(SECOND, "placeholder=flow")
        export = "Intro.\n\nBaton-gdoc-diagram-flow\n\nEnd.\n"
        new, _ = self.pull(current, export, document("Intro.", "Baton-gdoc-diagram-flow", "End."))
        self.assertEqual(new, doc_id_in("Intro.\n\n" + block(SECOND, "placeholder=flow") + "\nEnd.\n"))

    def test_placeholder_without_pending_mark_is_kept_as_text(self) -> None:
        export = "Intro.\n\nbaton-gdoc-diagram-later\n"
        new, notes = self.pull("", export, document("Intro.", "baton-gdoc-diagram-later"))
        self.assertEqual(new, doc_id_in(export))
        self.assertEqual(notes, [])

    def test_unmarked_image_becomes_a_note(self) -> None:
        export = "Shot ![][image1] here.\n\n[image1]: <data:image/png;base64,AAAA>\n"
        new, notes = self.pull("", export, document(P("Shot ", image("kix.shot"), " here.")))
        self.assertEqual(new, doc_id_in("Shot *(image kix.shot not archived)* here.\n"))
        self.assertTrue(any("kix.shot" in note for note in notes))

    def test_deleted_marked_image_drops_its_block(self) -> None:
        current = block(FIRST, image_mark("kix.a", FIRST)) + "\n" + block(SECOND, image_mark("kix.b", SECOND))
        export = "Text.\n\n![][image1]\n\n[image1]: <data:image/png;base64,AAAA>\n"
        new, notes = self.pull(current, export, document("Text.", P(image("kix.b"))))
        self.assertEqual(new, doc_id_in("Text.\n\n" + block(SECOND, image_mark("kix.b", SECOND))))
        self.assertTrue(any("Dropped" in note and "kix.a" in note for note in notes))

    def test_deleted_marked_image_with_unmarked_image_refuses(self) -> None:
        current = block(FIRST, image_mark("kix.a", FIRST))
        export = "Before the paste.\n\n![][image1]\n"
        self.refused(current, export, document("Before the paste.", P(image("kix.new"))),
                     "may have been cut and pasted", 'kix.new after "Before the paste."')

    def test_pending_placeholder_missing_or_repeated_refuses(self) -> None:
        current = block(FIRST, "placeholder=flow")
        for doc, export, expected in (
                (document("Text.", P(image("kix.pushed"))), "Text.\n\n![][image1]\n", "is missing from the Doc"),
                (document("baton-gdoc-diagram-flow", "baton-gdoc-diagram-flow"),
                 "baton-gdoc-diagram-flow\n\nbaton-gdoc-diagram-flow\n", "appears 2 times")):
            with self.subTest(expected=expected):
                message = self.refused(current, export, doc, expected, "image=ID")
                if "missing" in expected:
                    self.assertIn('kix.pushed after "Text."', message)

    def test_marked_image_not_alone_in_a_top_level_paragraph_refuses(self) -> None:
        current = block(FIRST, image_mark("kix.a", FIRST))
        cases = {
            "inline": (document(P("See ", image("kix.a"), " here.")), "See ![][image1] here.\n"),
            "table": (document(Table([P("cell"), P(image("kix.a"))])), "| cell | ![][image1] |\n| :- | :- |\n"),
            "list": (document(P(image("kix.a"), bullet=True)), "- ![][image1]\n"),
        }
        for name, (doc, export) in cases.items():
            with self.subTest(name=name):
                self.refused(current, export, doc, "not alone in its own paragraph")

    def test_reference_count_mismatch_refuses(self) -> None:
        self.refused("", "![][image1]\n\n![][image2]\n", document(P(image("kix.a"))), "text-wrapped", "In line")

    def test_marked_blocks_follow_their_images_when_sections_move(self) -> None:
        current = ("# One\n\n" + block(FIRST, image_mark("kix.a", FIRST)) + "\n# Two\n\n"
                   + block(SECOND, image_mark("kix.b", SECOND)))
        export = "# Two\n\n![][image1]\n\n# One\n\n![][image2]\n"
        doc = document(P("Two", style="HEADING_1"), P(image("kix.b")), P("One", style="HEADING_1"), P(image("kix.a")))
        new, _ = self.pull(current, export, doc)
        self.assertEqual(new, doc_id_in("# Two\n\n" + block(SECOND, image_mark("kix.b", SECOND)) + "\n# One\n\n"
                                        + block(FIRST, image_mark("kix.a", FIRST))))

    def test_mark_must_directly_precede_a_mermaid_fence_outside_other_fences(self) -> None:
        for current in ("<!-- baton-gdoc image=kix.a -->\n\n```mermaid\nx\n```\n",
                        "<!-- baton-gdoc image=kix.a -->\n```python\nx\n```\n```mermaid\nx\n```\n",
                        "````\n<!-- baton-gdoc image=kix.a -->\n```mermaid\nx\n```\n````\n```mermaid\ny\n```\n"):
            with self.subTest(current=current):
                with self.assertRaisesRegex(ProjectError, "Unmarked Mermaid block"):
                    self.pull(current, "Text.\n", document("Text."))
        # Inside an outer fence the lines are plain code, so the file has no Mermaid block at all.
        new, _ = self.pull("````\n<!-- baton-gdoc image=kix.a -->\n```mermaid\nx\n```\n````\n", "Text.\n",
                           document("Text."))
        self.assertEqual(new, doc_id_in("Text.\n"))

    def test_mermaid_fences_inside_containers_refuse(self) -> None:
        for current in ("> ```mermaid\n> x\n> ```\n", "- item\n\n    ```mermaid\n    x\n    ```\n",
                        "1. ```mermaid\n   x\n   ```\n"):
            with self.subTest(current=current):
                with self.assertRaisesRegex(ProjectError, "only at the top level"):
                    self.pull(current, "Text.\n", document("Text."))
                with self.assertRaisesRegex(ProjectError, "only at the top level"):
                    adopt_text(current, read_structure(document("Text.")), DOC)
        # A quoted fence inside a code block is code, not a diagram.
        new, _ = self.pull("```text\n> ```mermaid\n```\n", "Text.\n", document("Text."))
        self.assertEqual(new, doc_id_in("Text.\n"))

    def test_unmarked_block_and_duplicate_marks_refuse(self) -> None:
        for current, expected in ((block(FIRST), "Unmarked Mermaid block at line 1"),
                                  (block(FIRST, "image=kix.a") + block(SECOND, "image=kix.a"), "image=kix.a"),
                                  (block(FIRST, "placeholder=x") + block(SECOND, "placeholder=x"), "placeholder=x")):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ProjectError, expected):
                    self.pull(current, "![][image1]\n", document(P(image("kix.a"))))

    def test_exported_mermaid_fence_loses_its_label(self) -> None:
        export = "```mermaid\nflowchart LR\n```\n\n~~~ Mermaid extra\ngraph\n~~~\n"
        new, notes = self.pull("", export, document("x"))
        self.assertEqual(new, doc_id_in("```\nflowchart LR\n```\n\n~~~\ngraph\n~~~\n"))
        self.assertEqual(sum("mermaid label" in note for note in notes), 2)

    def test_image_definitions_inside_code_blocks_are_kept(self) -> None:
        export = "```\n[image1]: <data:image/png;base64,QUJD>\n```\n\n[image1]: <data:image/png;base64,AAAA>\n"
        new, _ = self.pull("", export, document("x"))
        self.assertEqual(new, doc_id_in("```\n[image1]: <data:image/png;base64,QUJD>\n```\n"))

    def test_spike_payload_counts_pending_suggestions_and_keeps_images_in_order(self) -> None:
        doc = json.loads((FIXTURES / "documents-get.json").read_text())
        export = (FIXTURES / "export.md").read_text()
        current = block(FIRST, image_mark("kix.f2nh8iq743kk", FIRST))
        new, notes = self.pull(current, export, doc)
        self.assertTrue(any("2 pending suggestion" in note for note in notes))
        self.assertIn("## Section C\n\n" + block(FIRST, image_mark("kix.f2nh8iq743kk", FIRST)), new)
        self.assertIn("Before *(image kix.8wdc5u149rnm not archived)* after.", new)
        self.assertIn("| cell text | *(image kix.saubuoroxzc9 not archived)* |", new)
        self.assertIn("SUGGESTEDINSERT", new)
        self.assertNotIn("data:image", new)

    def test_restored_blocks_are_set_apart_from_text_joined_by_hard_line_breaks(self) -> None:
        current = block(FIRST, image_mark("kix.a", FIRST)) + "\n" + block(SECOND, "placeholder=flow")
        doc = document("Intro.", P(image("kix.a")), "baton-gdoc-diagram-flow", "End.")
        hard = "Intro.  \n![][image1]  \nBaton-gdoc-diagram-flow  \nEnd.\n\n[image1]: <data:image/png;base64,AAAA>\n"
        new, _ = self.pull(current, hard, doc)
        self.assertEqual(new, doc_id_in("Intro.  \n\n" + block(FIRST, image_mark("kix.a", FIRST)) + "\n"
                                        + block(SECOND, "placeholder=flow") + "\nEnd.\n"))
        # Pulling the same export onto the new archive finds both marks and changes nothing.
        self.assertEqual(self.pull(new, hard, doc)[0], new)
        # Blocks already set apart by blank lines come out as before.
        spaced = "Intro.\n\n![][image1]\n\nBaton-gdoc-diagram-flow\n\nEnd.\n"
        self.assertEqual(self.pull(current, spaced, doc)[0],
                         doc_id_in("Intro.\n\n" + block(FIRST, image_mark("kix.a", FIRST)) + "\n"
                                   + block(SECOND, "placeholder=flow") + "\nEnd.\n"))

    def test_image_context_starts_at_a_word(self) -> None:
        text = "Each widget carries an API-inserted ID. Rejected widgets are kept for fourteen days."
        structure = read_structure(document(text, P(image("kix.x"))))
        self.assertEqual(structure.images[0].before, "ID. Rejected widgets are kept for fourteen days.")
        self.assertEqual(context("Short   text.\n"), "Short text.")
        self.assertEqual(context("cut " + "a" * 55 + " word"), "a" * 55 + " word")
        self.assertEqual(context("x" * 70), "x" * 60)

    def test_only_a_definite_revision_rejection_counts_as_nothing_written(self) -> None:
        rejected = "The required revision ID 'ALBJ4Ls' does not match the latest revision."
        self.assertTrue(revision_rejected(ProjectError(
            f"gws docs documents batchUpdate failed (exit 1): error[api]: {rejected}\n{{\"code\": 400}}")))
        for error in (ProjectError("gws docs documents batchUpdate failed (exit 1): deadline exceeded"),
                      ProjectError(f"gws docs documents get failed (exit 1): {rejected}"),
                      ProjectError("gws docs documents batchUpdate failed (exit 1): required revision ID does not match"),
                      OSError(rejected)):
            with self.subTest(error=error):
                self.assertFalse(revision_rejected(error))
        with tempfile.TemporaryDirectory() as directory:
            slow = Path(directory) / "gws"
            slow.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(5)\n")
            slow.chmod(0o755)
            with mock.patch.dict(os.environ, GWS_BIN=str(slow)), self.assertRaises(ProjectError) as caught:
                gws("docs documents batchUpdate", {"documentId": DOC}, {"requests": []}, timeout=1)
        self.assertIn("timed out", str(caught.exception))
        self.assertFalse(revision_rejected(caught.exception))

    def test_placeholders_match_any_case_but_not_in_tables_or_lists(self) -> None:
        structure = read_structure(document("BATON-GDOC-DIAGRAM-top", Table([P("baton-gdoc-diagram-cell")]),
                                            P("baton-gdoc-diagram-item", bullet=True)))
        self.assertEqual(list(structure.placeholders), ["top"])
        current = block(FIRST, "placeholder=cell")
        self.refused(current, "| baton-gdoc-diagram-cell |\n| :- |\n",
                     document(Table([P("baton-gdoc-diagram-cell")])), "is missing from the Doc")

    def test_rendered_size_accounts_for_the_render_scale_and_text_width(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + bytes(8) + (800).to_bytes(4, "big") + (400).to_bytes(4, "big")
        self.assertEqual(image_size(png, 468)["width"]["magnitude"], 300)
        self.assertEqual(image_size(png, 200)["height"]["magnitude"], 100)

    def test_adopt_pairs_unmarked_blocks_with_unnamed_images_in_order(self) -> None:
        current = ("# T\n\n" + block(FIRST) + "\ntext\n\n" + block(SECOND, "image=kix.b") + "\n" + block(THIRD))
        doc = document(P(image("kix.a")), P(image("kix.b")), P(image("kix.c")))
        new = adopt_text(current, read_structure(doc), DOC)
        self.assertEqual(new, doc_id_in("# T\n\n" + block(FIRST, image_mark("kix.a", FIRST)) + "\ntext\n\n"
                                        + block(SECOND, "image=kix.b") + "\n" + block(THIRD, image_mark("kix.c", THIRD))))
        # A second adoption leaves one header and the marks as they are.
        self.assertEqual(adopt_text(new, read_structure(doc), DOC), new)


class GdocCommandTest(unittest.TestCase):
    run_command = fixtures.ProjectManagerTest.run_command
    git = fixtures.ProjectManagerTest.git
    cli = fixtures.ProjectManagerTest.cli
    make_repo = fixtures.ProjectManagerTest.make_repo
    create = fixtures.ProjectManagerTest.create
    checkout = fixtures.ProjectManagerTest.checkout

    def setUp(self) -> None:
        fixtures.ProjectManagerTest.setUp(self)
        self.make_repo("alpha")
        self.create("alpha")
        self.directory = artifact_path(self.projects, self.artifacts, "example")
        self.directory.mkdir(parents=True, exist_ok=True)
        bindir = self.root / "fake-bin"
        bindir.mkdir()
        self.state = self.root / "gws-state.json"
        self.state.write_text(json.dumps({"log": [], "responses": {}}))
        self.mmdc_log = self.root / "mmdc-log.json"
        self.mmdc_log.write_text("[]")
        self.mmdc_fail = self.root / "mmdc-fail"
        gws = bindir / "gws"
        gws.write_text(f"#!{sys.executable}\nSTATE = {str(self.state)!r}\n" + FAKE_GWS)
        mmdc = bindir / "mmdc"
        mmdc.write_text(f"#!{sys.executable}\nLOG = {str(self.mmdc_log)!r}\nFAIL = {str(self.mmdc_fail)!r}\n" + FAKE_MMDC)
        for executable in (gws, mmdc):
            executable.chmod(0o755)
        self.env.update(GWS_BIN=str(gws), MMDC_BIN=str(mmdc))

    def respond(self, command: str, *responses: dict[str, Any], target: str | None = None) -> None:
        state = json.loads(self.state.read_text())
        state["responses"][command if target is None else f"{command} {target}"] = list(responses)
        self.state.write_text(json.dumps(state))

    def serve(self, doc: dict[str, Any], export: str = "", target: str = DOC, revision: str = "r1") -> None:
        self.respond("docs documents get", {"stdout": doc}, target=target)
        self.respond("revision", {"stdout": {"revisionId": revision}}, target=target)
        self.respond("drive files export", {"file": export}, target=target)

    def calls(self, command: str | None = None) -> list[dict[str, Any]]:
        return [entry for entry in json.loads(self.state.read_text())["log"] if command in (None, entry["command"])]

    def rendered(self) -> list[str]:
        return json.loads(self.mmdc_log.read_text())

    def gdoc(self, *args: str, ok: bool = True, cwd: Path | None = None) -> Any:
        return self.cli("gdoc", *args, "--selector", "example", ok=ok, cwd=cwd)

    def commits(self) -> int:
        return int(self.git(self.artifacts, "rev-list", "--count", "HEAD"))

    def commit_files(self, message: str, *paths: Path) -> None:
        self.git(self.artifacts, "add", "--", *map(str, paths))
        self.git(self.artifacts, "commit", "--allow-empty", "-m", message)

    def linked(self, name: str, body: str, document_id: str = DOC, links: list[tuple[str, str]] | None = None) -> Path:
        path = self.directory / name
        path.write_text(header(document_id) + "\n\n" + body)
        records = [{"path": n, "document_id": d} for n, d in (links or [(name, document_id)])]
        (self.directory / "gdoc-links.json").write_text(json.dumps({"links": records}))
        self.commit_files(f"Link {name}", path, self.directory / "gdoc-links.json")
        return path

    def run_printed(self, stderr: str, prefix: str) -> list[str]:
        """Run the command printed after `prefix` from a directory outside the project, as a user might."""
        (line,) = [line for line in stderr.splitlines() if prefix in line]
        words = shlex.split(line.split(prefix, 1)[1])
        uuid = json.loads(self.cli("status", "--selector", "example", "--json").stdout)["uuid"]
        self.assertEqual(words[0], "baton")
        self.assertIn(["--selector", uuid], [words[n:n + 2] for n in range(len(words))])
        outside = self.root / "elsewhere"
        outside.mkdir(exist_ok=True)
        self.cli(*words[1:], cwd=outside)
        return words

    def add_origin(self) -> Path:
        origin = self.root / "artifacts.git"
        self.git(self.root, "init", "--bare", "-b", "main", str(origin))
        self.git(self.artifacts, "remote", "add", "origin", str(origin))
        self.git(self.artifacts, "push", "-u", "origin", "main")
        self.git(self.artifacts, "remote", "set-head", "origin", "-a")
        return origin

    # pull

    def test_pull_commits_once_per_link_and_not_again_when_unchanged(self) -> None:
        first = self.directory / "one.md"
        second = self.directory / "two.md"
        links = [("one.md", DOC), ("two.md", OTHER)]
        first.write_text(header(DOC) + "\n\nold\n\n" + block(FIRST, image_mark("kix.a", FIRST)))
        second.write_text(header(OTHER) + "\n\nold two\n")
        (self.directory / "gdoc-links.json").write_text(json.dumps(
            {"links": [{"path": n, "document_id": d} for n, d in links]}))
        self.commit_files("Link both", first, second, self.directory / "gdoc-links.json")
        self.serve(document("New text.", P(image("kix.a"))),
                   "New text.\n\n![][image1]\n\n[image1]: <data:image/png;base64,AAAA>\n")
        self.serve(document("Second."), "Second.\n", target=OTHER)
        before = self.commits()
        self.gdoc("pull")
        self.assertEqual(self.commits(), before + 2)
        self.assertEqual(first.read_text(), header(DOC) + "\n\nNew text.\n\n" + block(FIRST, image_mark("kix.a", FIRST)))
        self.assertEqual(second.read_text(), header(OTHER) + "\n\nSecond.\n")
        for path in (first, second):
            self.assertEqual(self.git(self.artifacts, "log", "-1", "--format=%s", "--", str(path)),
                             f"Pull {path.name} from its Google Doc")
        for _ in range(3):
            result = self.gdoc("pull")
            self.assertIn("unchanged", result.stdout)
        self.assertEqual(self.commits(), before + 2)
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")
        self.assertFalse(self.calls("docs documents batchUpdate"))

    def test_pull_of_hard_line_breaks_is_unchanged_the_second_time(self) -> None:
        path = self.linked("spec.md", "old\n\n" + block(FIRST, image_mark("kix.a", FIRST)))
        self.serve(document("Intro.", P(image("kix.a")), "End."), "Intro.  \n![][image1]  \nEnd.\n")
        self.gdoc("pull")
        self.assertEqual(path.read_text(), header(DOC) + "\n\nIntro.  \n\n" + block(FIRST, image_mark("kix.a", FIRST))
                         + "\nEnd.\n")
        self.assertIn("unchanged", self.gdoc("pull").stdout)

    def test_pull_syncs_and_pushes_once_and_keeps_commits_when_fetch_fails(self) -> None:
        first = self.linked("one.md", "old\n")
        second = self.directory / "two.md"
        second.write_text(header(OTHER) + "\n\nold two\n")
        (self.directory / "gdoc-links.json").write_text(json.dumps({"links": [
            {"path": "one.md", "document_id": DOC}, {"path": "two.md", "document_id": OTHER}]}))
        self.commit_files("Link both", second, self.directory / "gdoc-links.json")
        origin = self.add_origin()
        self.serve(document("New."), "New.\n")
        self.serve(document("Second."), "Second.\n", target=OTHER)
        result = self.gdoc("pull")
        self.assertEqual(result.stdout.count("Pushed"), 1)
        self.assertIn("Pushed 2 artifact commits", result.stdout)
        self.assertEqual(result.stderr.count("fetch origin"), 1)
        self.assertEqual(self.git(origin, "rev-parse", "main"), self.git(self.artifacts, "rev-parse", "HEAD"))
        self.serve(document("Newer."), "Newer.\n")
        self.git(self.artifacts, "remote", "set-url", "origin", str(self.root / "missing.git"))
        result = self.gdoc("pull", str(first))
        self.assertIn("Push skipped because fetch failed", result.stderr)
        self.assertEqual(first.read_text(), header(DOC) + "\n\nNewer.\n")
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")

    def test_refused_link_is_skipped_while_others_commit(self) -> None:
        first = self.directory / "one.md"
        second = self.directory / "two.md"
        first.write_text(header(DOC) + "\n\nold\n")
        second.write_text(header(OTHER) + "\n\nold two\n")
        (self.directory / "gdoc-links.json").write_text(json.dumps({"links": [
            {"path": "one.md", "document_id": DOC}, {"path": "two.md", "document_id": OTHER}]}))
        self.commit_files("Link both", first, second, self.directory / "gdoc-links.json")
        self.serve(document("x"), "![][image1]\n")
        self.serve(document("Second."), "Second.\n", target=OTHER)
        result = self.gdoc("pull", ok=False)
        self.assertIn("one.md: not pulled", result.stderr)
        self.assertEqual(first.read_text(), header(DOC) + "\n\nold\n")
        self.assertEqual(second.read_text(), header(OTHER) + "\n\nSecond.\n")
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")

    def test_pull_refuses_to_overwrite_uncommitted_changes(self) -> None:
        path = self.linked("spec.md", "old\n")
        path.write_text(path.read_text() + "local edit\n")
        self.serve(document("New."), "New.\n")
        result = self.gdoc("pull", str(path), ok=False)
        self.assertIn("uncommitted changes", result.stderr)
        self.assertTrue(path.read_text().endswith("local edit\n"))

    def test_pull_and_push_refuse_a_staged_change_hidden_by_a_clean_working_file(self) -> None:
        path, doc = self.three_diagrams()
        committed = path.read_text()
        path.write_text(committed + "staged edit\n")
        self.git(self.artifacts, "add", str(path))
        path.write_text(committed)
        self.serve(doc, "# Spec\n\n![][image1]\n\n![][image2]\n\nBaton-gdoc-diagram-new\n")
        self.assertIn("staged changes", self.gdoc("pull", str(path), ok=False).stderr)
        self.serve_push(doc)
        self.assertIn("uncommitted changes", self.gdoc("push", str(path), ok=False).stderr)
        self.assertEqual(self.git(self.artifacts, "show", f":{path.relative_to(self.artifacts)}"),
                         (committed + "staged edit").rstrip("\n"))
        self.assertFalse(self.calls("docs documents batchUpdate"))

    def test_pull_refuses_when_markdown_changes_during_the_export(self) -> None:
        path = self.linked("spec.md", "old\n")
        racing = header(DOC) + "\n\nnewer archive\n"
        self.respond("drive files export", {"file": "New.\n", "write": {str(path): racing}, "run": [
            ["git", "-C", str(self.artifacts), "commit", "-qam", "Another pull"]]}, target=DOC)
        self.respond("docs documents get", {"stdout": document("New.")}, target=DOC)
        self.respond("revision", {"stdout": {"revisionId": "r1"}}, target=DOC)
        result = self.gdoc("pull", ok=False)
        self.assertIn("changed while the Doc was exported", result.stderr)
        self.assertEqual(path.read_text(), racing)

    def test_pull_retries_once_when_the_revision_moves_then_refuses(self) -> None:
        path = self.linked("spec.md", "old\n")
        self.serve(document("New."), "New.\n")
        # The first read is the revision call; the second is the revision of the structure read after the export.
        self.respond("revision", {"stdout": {"revisionId": "r1"}}, {"stdout": {"revisionId": "r3"}}, target=DOC)
        self.respond("docs documents get", {"stdout": document("New.", revision="r2")},
                     {"stdout": document("New.", revision="r4")}, target=DOC)
        result = self.gdoc("pull", ok=False)
        self.assertIn("changed during the export twice", result.stderr)
        self.assertEqual(len(self.calls("drive files export")), 2)
        self.assertEqual(path.read_text(), header(DOC) + "\n\nold\n")
        self.respond("revision", {"stdout": {"revisionId": "r1"}}, {"stdout": {"revisionId": "r2"}}, target=DOC)
        self.respond("docs documents get", {"stdout": document("New.", revision="r9")},
                     {"stdout": document("New.", revision="r2")}, target=DOC)
        self.gdoc("pull")
        self.assertEqual(path.read_text(), header(DOC) + "\n\nNew.\n")

    def test_rerun_completes_a_pull_whose_commit_failed(self) -> None:
        path = self.linked("spec.md", "old\n")
        self.serve(document("New."), "New.\n")
        lock = self.artifacts / ".git" / "index.lock"
        lock.write_text("")
        before = self.commits()
        result = self.gdoc("pull", ok=False)
        self.assertIn("Artifact commit failed", result.stderr)
        self.assertEqual(path.read_text(), header(DOC) + "\n\nNew.\n")
        self.assertEqual(self.commits(), before)
        lock.unlink()
        self.gdoc("pull")
        self.assertEqual(self.commits(), before + 1)
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")

    def test_pull_dry_run_prints_the_diff_and_writes_nothing(self) -> None:
        path = self.linked("spec.md", "old\n")
        self.serve(document("New."), "New.\n")
        before = path.read_text()
        self.env.pop("BATON_ALLOW_UNTRUSTED_WRITES")
        result = self.gdoc("pull", "--dry-run")
        self.assertIn("1 lines added, 1 removed", result.stdout)
        self.assertIn("-old\n+New.", result.stdout)
        self.assertEqual(path.read_text(), before)

    # push

    def three_diagrams(self) -> tuple[Path, dict[str, Any]]:
        body = ("# Spec\n\n" + block(FIRST, image_mark("kix.a", FIRST)) + "\n"
                + block(SECOND, image_mark("kix.b", FIRST)) + "\n" + block(THIRD, "placeholder=new"))
        path = self.linked("spec.md", body)
        doc = document(P("Spec", style="HEADING_1"), P(image("kix.a")), P(image("kix.b")), "Baton-gdoc-diagram-new")
        return path, doc

    def serve_push(self, doc: dict[str, Any], *batch: dict[str, Any], new_ids: tuple[str, ...] = ("kix.n1", "kix.n2")) -> None:
        self.respond("docs documents get", {"stdout": doc}, target=DOC)
        self.respond("drive files create", *[{"stdout": {"id": f"tmp-{n}"}} for n in range(len(new_ids))])
        for n in range(len(new_ids)):
            self.respond("docs documents get", {"stdout": {"tabs": [{"documentTab": {"inlineObjects": {"x": {
                "inlineObjectProperties": {"embeddedObject": {"imageProperties": {"contentUri": f"uri-{n}"}}}}}}}]}},
                target=f"tmp-{n}")
        replies = [item for image_id in new_ids for item in ({}, {"insertInlineImage": {"objectId": image_id}})]
        self.respond("docs documents batchUpdate", *(batch or ({"stdout": {"replies": replies}},)))
        self.respond("drive files delete", {})

    def test_push_renders_only_pending_and_stale_diagrams_and_commits_new_marks(self) -> None:
        path, doc = self.three_diagrams()
        self.serve_push(doc)
        before = self.commits()
        self.gdoc("push", str(path))
        self.assertEqual(self.rendered(), [SECOND, THIRD])
        (update,) = self.calls("docs documents batchUpdate")
        self.assertEqual(update["body"]["writeControl"], {"requiredRevisionId": "r1"})
        requests = update["body"]["requests"]
        placeholder = doc["tabs"][0]["documentTab"]["body"]["content"][4]["paragraph"]["elements"][0]
        stale = doc["tabs"][0]["documentTab"]["body"]["content"][3]["paragraph"]["elements"][0]["startIndex"]
        self.assertEqual([next(iter(request)) for request in requests],
                         ["deleteContentRange", "insertInlineImage", "deleteContentRange", "insertInlineImage"])
        self.assertEqual(requests[0]["deleteContentRange"]["range"],
                         {"startIndex": placeholder["startIndex"], "endIndex": placeholder["endIndex"], "tabId": "t.0"})
        self.assertEqual(requests[2]["deleteContentRange"]["range"], {"startIndex": stale, "endIndex": stale + 1, "tabId": "t.0"})
        self.assertEqual(requests[1]["insertInlineImage"]["uri"], "uri-1")
        self.assertEqual(path.read_text(), header(DOC) + "\n\n# Spec\n\n" + block(FIRST, image_mark("kix.a", FIRST))
                         + "\n" + block(SECOND, image_mark("kix.n2", SECOND)) + "\n" + block(THIRD, image_mark("kix.n1", THIRD)))
        self.assertEqual(self.commits(), before + 1)
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")
        self.assertEqual(sorted(call["params"]["fileId"] for call in self.calls("drive files delete")), ["tmp-0", "tmp-1"])
        self.assertTrue(all(call["upload_type"] == "image/png" for call in self.calls("drive files create")))
        result = self.gdoc("push", str(path))
        self.assertIn("no pending or changed diagrams", result.stdout)

    def test_push_checks_refuse_before_any_doc_change(self) -> None:
        path, doc = self.three_diagrams()
        good = path.read_text()
        content = doc["tabs"][0]["documentTab"]["body"]["content"]
        cases = {
            "uncommitted": (good + "edit\n", doc, "uncommitted changes"),
            "missing image": (good, document(P(image("kix.b")), "baton-gdoc-diagram-new"), "pull first"),
            "missing placeholder": (good, document(P(image("kix.a")), P(image("kix.b"))), "found 0"),
            "repeated placeholder": (good, document(*[P(image(i)) for i in ("kix.a", "kix.b")],
                                                    "baton-gdoc-diagram-new", "baton-gdoc-diagram-new"), "found 2"),
            "placeholder in a list": (good, document(P(image("kix.a")), P(image("kix.b")),
                                                     P("baton-gdoc-diagram-new", bullet=True)), "found 0"),
            "unmarked block": (good + "\n" + block("graph\n"), doc, "Unmarked Mermaid block"),
        }
        self.assertTrue(content)
        for name, (text, served, expected) in cases.items():
            with self.subTest(name=name):
                path.write_text(text)
                if name != "uncommitted":
                    self.commit_files(name, path)
                self.serve_push(served)
                result = self.gdoc("push", str(path), ok=False)
                self.assertIn(expected, result.stderr)
                self.assertFalse(self.calls("docs documents batchUpdate"))
                self.assertFalse(self.calls("drive files create"))
                self.assertEqual(self.rendered(), [])
                path.write_text(good)
                self.commit_files(f"Restore after {name}", path)

    def test_push_refuses_a_failed_fetch_or_a_stale_checkout_without_google_calls(self) -> None:
        path, doc = self.three_diagrams()
        origin = self.add_origin()
        self.serve_push(doc)
        self.git(self.artifacts, "remote", "set-url", "origin", str(self.root / "missing.git"))
        result = self.gdoc("push", str(path), ok=False)
        self.assertIn("Fetching the artifact checkout failed", result.stderr)
        self.assertEqual(self.calls(), [])
        self.git(self.artifacts, "remote", "set-url", "origin", str(origin))
        other = self.root / "other-artifacts"
        self.git(self.root, "clone", str(origin), str(other))
        (other / "elsewhere.md").write_text("remote work\n")
        self.git(other, "add", "elsewhere.md")
        self.git(other, "commit", "-m", "Remote work")
        self.git(other, "push", "origin", "main")
        (self.artifacts / "local.md").write_text("local work\n")
        self.git(self.artifacts, "add", "local.md")
        self.git(self.artifacts, "commit", "-m", "Local work")
        result = self.gdoc("push", str(path), ok=False)
        self.assertIn("behind origin/main", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_commit_failure_after_the_doc_changed_keeps_the_new_marks(self) -> None:
        path, doc = self.three_diagrams()
        lock = self.artifacts / ".git" / "index.lock"
        replies = [{}, {"insertInlineImage": {"objectId": "kix.n1"}}, {}, {"insertInlineImage": {"objectId": "kix.n2"}}]
        self.serve_push(doc, {"stdout": {"replies": replies}, "write": {str(lock): ""}})
        result = self.gdoc("push", str(path), ok=False)
        self.assertIn("The Doc was updated", result.stderr)
        self.assertIn("image=kix.n1", result.stderr)
        self.assertIn("image=kix.n2", result.stderr)
        self.assertIn(block(SECOND, image_mark("kix.n2", SECOND)), path.read_text())
        lock.unlink()
        self.run_printed(result.stderr, "Fix the cause shown above, then commit it with: ")
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")
        self.serve(document(P(image("kix.a")), P(image("kix.n2")), P(image("kix.n1"))),
                   "![][image1]\n\n![][image2]\n\n![][image3]\n")
        self.gdoc("pull")
        self.assertIn("no pending or changed diagrams", self.gdoc("push", str(path)).stdout)

    def test_applied_but_failed_batch_update_lists_new_ids_and_recovers(self) -> None:
        body = "Intro.\n\n" + block(FIRST, "placeholder=flow")
        path = self.linked("spec.md", body)
        before = document("Intro.", "baton-gdoc-diagram-flow")
        after = document("Intro.", P(image("kix.landed")), revision="r2")
        self.serve_push(before, {"exit": 1, "stdout": {"error": "deadline exceeded"}}, new_ids=("kix.landed",))
        self.respond("docs documents get", {"stdout": before}, {"stdout": after}, target=DOC)
        result = self.gdoc("push", str(path), ok=False)
        self.assertIn("may still have been applied", result.stderr)
        self.assertIn('kix.landed after "Intro."', result.stderr)
        self.assertEqual(path.read_text(), header(DOC) + "\n\n" + body)
        path.write_text(header(DOC) + "\n\nIntro.\n\n" + block(FIRST, "image=kix.landed"))
        self.commit_files("Record the landed image", path)
        self.serve(after, "Intro.\n\n![][image1]\n", revision="r2")
        self.gdoc("pull")
        self.assertIn(block(FIRST, "image=kix.landed"), path.read_text())
        self.serve_push(after, new_ids=("kix.final",))
        self.gdoc("push", str(path))
        self.assertIn(block(FIRST, image_mark("kix.final", FIRST)), path.read_text())
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")

    def test_push_rejected_for_a_moved_revision_says_nothing_was_written(self) -> None:
        path, doc = self.three_diagrams()
        committed = path.read_text()
        rejection = {"code": 400, "message": "The required revision ID 'r1' does not match the latest revision.",
                     "reason": "unknown"}
        self.serve_push(doc, {"exit": 1, "stdout": {"error": rejection}})
        before = self.commits()
        result = self.gdoc("push", str(path), ok=False)
        self.assertIn("baton gdoc: The Doc changed while the push ran (an edit or a comment counts), so Google refused "
                      "the update: nothing was written to the Doc or the Markdown. Push again.", result.stderr)
        self.assertNotIn("may still have been applied", result.stderr)
        self.assertNotIn("Unmarked images", result.stderr)
        self.assertEqual(len([call for call in self.calls("docs documents get") if call["params"]["documentId"] == DOC]), 1)
        self.assertEqual(path.read_text(), committed)
        self.assertEqual(self.commits(), before)
        self.assertEqual(sorted(call["params"]["fileId"] for call in self.calls("drive files delete")), ["tmp-0", "tmp-1"])
        self.serve_push(doc)
        self.gdoc("push", str(path))
        self.assertIn(block(THIRD, image_mark("kix.n1", THIRD)), path.read_text())

    def test_push_dry_run_lists_diagrams_and_changes_nothing(self) -> None:
        path, doc = self.three_diagrams()
        self.serve_push(doc)
        self.env.pop("BATON_ALLOW_UNTRUSTED_WRITES")
        result = self.gdoc("push", str(path), "--dry-run")
        self.assertIn("would render the block at line 12 (image=kix.b)", result.stdout)
        self.assertIn("placeholder=new", result.stdout)
        self.assertEqual([call["command"] for call in self.calls()], ["docs documents get"])
        self.assertEqual(self.rendered(), [])

    # link and unlink

    def test_adopt_marks_blocks_in_order_and_writes_nothing_to_the_doc(self) -> None:
        path = self.directory / "spec.md"
        path.write_text("# Spec\n\n" + block(FIRST) + "\nMiddle.\n\n" + block(SECOND))
        self.respond("docs documents get", {"stdout": document(
            "Spec", P(image("kix.a")), "Middle.", P(image("kix.b")))}, target=DOC)
        before = self.commits()
        self.gdoc("link", str(path), f"https://docs.google.com/document/d/{DOC}/edit?tab=t.0")
        self.assertEqual(path.read_text(), header(DOC) + "\n\n# Spec\n\n" + block(FIRST, image_mark("kix.a", FIRST))
                         + "\nMiddle.\n\n" + block(SECOND, image_mark("kix.b", SECOND)))
        self.assertEqual(json.loads((self.directory / "gdoc-links.json").read_text()),
                         {"links": [{"path": "spec.md", "document_id": DOC}]})
        self.assertEqual([call["command"] for call in self.calls()], ["docs documents get"])
        self.assertEqual(self.commits(), before + 1)
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")

    def test_adopt_leaves_hand_marked_blocks_alone(self) -> None:
        path = self.directory / "spec.md"
        path.write_text(block(FIRST, "image=kix.b") + "\n" + block(SECOND))
        self.respond("docs documents get", {"stdout": document(P(image("kix.a")), P(image("kix.b")))}, target=DOC)
        # Relative to the caller's directory, although gws runs from a temporary one.
        self.env["GWS_BIN"] = os.path.relpath(self.env["GWS_BIN"], self.root)
        self.gdoc("link", str(path), DOC)
        self.assertEqual(path.read_text(), header(DOC) + "\n\n" + block(FIRST, "image=kix.b") + "\n"
                         + block(SECOND, image_mark("kix.a", SECOND)))

    def test_doc_with_a_diagram_and_another_image_links_once_the_block_is_marked(self) -> None:
        path = self.directory / "spec.md"
        path.write_text("Shot:\n\n" + block(FIRST))
        self.respond("docs documents get", {"stdout": document(
            "A screenshot", P(image("kix.shot")), "The diagram", P(image("kix.diagram")))}, target=DOC)
        result = self.gdoc("link", str(path), DOC, ok=False)
        self.assertIn("cannot be paired", result.stderr)
        self.assertIn('kix.shot after "A screenshot"', result.stderr)
        self.assertIn('kix.diagram after "A screenshot The diagram"', result.stderr)
        self.assertFalse((self.directory / "gdoc-links.json").exists())
        path.write_text("Shot:\n\n" + block(FIRST, "image=kix.diagram"))
        self.gdoc("link", str(path), DOC)
        self.assertEqual(path.read_text(), header(DOC) + "\n\nShot:\n\n" + block(FIRST, "image=kix.diagram"))

    def test_link_refusals_write_nothing(self) -> None:
        linked = self.linked("linked.md", "text\n")
        path = self.directory / "spec.md"
        path.write_text(block(FIRST))
        outside = self.root / "outside.md"
        outside.write_text("x\n")
        alias = self.directory / "alias.md"
        alias.symlink_to(path)
        self.respond("docs documents get", {"stdout": document(P(image("kix.a")), tabs=2)}, target=OTHER)
        self.respond("docs documents get", {"stdout": document(P(image("kix.a")), P(image("kix.b")))},
                     target="doc-gamma-00000000000000000")
        cases = {
            "multi-tab": ((str(path), OTHER), "several tabs"),
            "count mismatch": ((str(path), "doc-gamma-00000000000000000"), "cannot be paired"),
            "linked path": ((str(linked), OTHER), "already linked"),
            "linked Doc": ((str(path), DOC), "already linked"),
            "outside": ((str(outside), OTHER), "outside"),
            "symlink": ((str(alias), OTHER), "symlinked"),
        }
        links = (self.directory / "gdoc-links.json").read_text()
        before = self.commits()
        for name, (args, expected) in cases.items():
            with self.subTest(name=name):
                result = self.gdoc("link", *args, ok=False)
                self.assertIn(expected, result.stderr)
                self.assertEqual(path.read_text(), block(FIRST))
                self.assertEqual((self.directory / "gdoc-links.json").read_text(), links)
                self.assertEqual(self.commits(), before)

    def test_create_imports_placeholders_then_pushes_the_diagrams(self) -> None:
        path = self.directory / "spec.md"
        path.write_text("# Design\n\nText.\n" + block(FIRST, "image=stale") + "More.\n")
        self.serve_push(document("Design", "Text.", "baton-gdoc-diagram-1", "More."), new_ids=("kix.made",))
        self.respond("drive files create", {"stdout": {"id": DOC}}, {"stdout": {"id": "tmp-0"}})
        result = self.gdoc("link", str(path))
        self.assertIn(f"Created https://docs.google.com/document/d/{DOC}/edit", result.stdout)
        imported = self.calls("drive files create")[0]
        self.assertEqual(imported["body"], {"name": "Design", "mimeType": "application/vnd.google-apps.document"})
        self.assertEqual(imported["upload_type"], "text/markdown")
        self.assertEqual(imported["upload"], "# Design\n\nText.\n\nbaton-gdoc-diagram-1\n\nMore.\n")
        self.assertEqual(path.read_text(), header(DOC) + "\n\n# Design\n\nText.\n"
                         + block(FIRST, image_mark("kix.made", FIRST)) + "More.\n")
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")

    def test_link_refuses_a_checkout_that_cannot_commit_before_calling_google(self) -> None:
        path = self.directory / "spec.md"
        path.write_text(block(FIRST))
        self.git(self.artifacts, "switch", "-c", "elsewhere")
        for args in (("link", str(path)), ("link", str(path), DOC)):
            with self.subTest(args=args):
                self.assertIn("committed only on main", self.gdoc(*args, ok=False).stderr)
        self.assertEqual(self.calls(), [])

    def test_create_leaves_pending_marks_when_its_push_fails(self) -> None:
        path = self.directory / "spec.md"
        path.write_text("Text.\n\n" + block(FIRST) + "\n" + block(SECOND))
        self.respond("drive files create", {"stdout": {"id": DOC}})
        self.respond("docs documents get", {"stdout": document(
            "Text.", "baton-gdoc-diagram-1", "baton-gdoc-diagram-2")}, target=DOC)
        self.mmdc_fail.write_text("")
        result = self.gdoc("link", str(path), "--title", "Chosen", ok=False)
        self.assertIn("diagrams are pending", result.stderr)
        self.assertEqual(self.calls("drive files create")[0]["body"]["name"], "Chosen")
        self.assertEqual(path.read_text(), header(DOC) + "\n\nText.\n\n" + block(FIRST, "placeholder=1") + "\n"
                         + block(SECOND, "placeholder=2"))
        self.assertEqual(json.loads((self.directory / "gdoc-links.json").read_text())["links"][0]["document_id"], DOC)
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")
        self.assertFalse(self.calls("docs documents batchUpdate"))

    def test_create_that_cannot_record_its_link_explains_how_to_adopt_the_doc(self) -> None:
        path = self.directory / "spec.md"
        path.write_text("Text.\n\n" + block(FIRST) + "\n" + block(SECOND))
        edited = "Edited meanwhile.\n\n" + block(FIRST) + "\n" + block(SECOND)
        self.respond("drive files create", {"stdout": {"id": DOC}, "write": {str(path): edited}})
        result = self.gdoc("link", str(path), ok=False)
        self.assertIn("changed while the command ran", result.stderr)
        self.assertIn("placeholder=1 to placeholder=2", result.stderr)
        self.assertFalse((self.directory / "gdoc-links.json").exists())
        path.write_text("Edited meanwhile.\n\n" + block(FIRST, "placeholder=1") + "\n" + block(SECOND, "placeholder=2"))
        doc = document("Edited meanwhile.", "baton-gdoc-diagram-1", "baton-gdoc-diagram-2")
        self.respond("docs documents get", {"stdout": doc}, target=DOC)
        words = self.run_printed(result.stderr, "Adopt it with: ")
        self.assertEqual(words[-2:], [str(path), f"https://docs.google.com/document/d/{DOC}/edit"])
        self.serve_push(doc)
        self.gdoc("push", str(path))
        self.assertEqual(path.read_text(), header(DOC) + "\n\nEdited meanwhile.\n\n" + block(FIRST, image_mark("kix.n2", FIRST))
                         + "\n" + block(SECOND, image_mark("kix.n1", SECOND)))

    def test_unlink_removes_header_marks_and_link(self) -> None:
        path = self.linked("spec.md", "Text.\n\n" + block(FIRST, image_mark("kix.a", FIRST)) + block(SECOND, "placeholder=x"))
        self.gdoc("unlink", str(path))
        self.assertEqual(path.read_text(), "Text.\n\n" + block(FIRST) + block(SECOND))
        self.assertFalse((self.directory / "gdoc-links.json").exists())
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")
        self.assertEqual(self.calls(), [])
        self.assertIn("not linked", self.gdoc("unlink", str(path), ok=False).stderr)

    def test_pull_and_push_from_a_branch_checkout_leave_other_subplans_alone(self) -> None:
        self.cli("branch", "add", "alpha", "docs", cwd=self.slots / "p3")
        docs = self.checkout("alpha", "example--docs")
        own = self.directory / "branches" / "alpha" / "example--docs" / "plan.md"
        other = self.directory / "branches" / "alpha" / "example" / "plan.md"
        for path in (own, other):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(header(DOC) + "\n\nold\n")
        names = [str(path.relative_to(self.directory)) for path in (own, other)]
        (self.directory / "gdoc-links.json").write_text(json.dumps({"links": [
            {"path": names[0], "document_id": DOC}, {"path": names[1], "document_id": OTHER}]}))
        self.commit_files("Link subplans", own, other, self.directory / "gdoc-links.json")
        self.serve(document("Mine."), "Mine.\n")
        self.serve(document("Theirs."), "Theirs.\n", target=OTHER)
        result = self.gdoc("pull", cwd=docs)
        self.assertIn(f"{names[1]}: skipped", result.stdout)
        self.assertEqual(own.read_text(), header(DOC) + "\n\nMine.\n")
        self.assertEqual(other.read_text(), header(DOC) + "\n\nold\n")
        for args in (("pull", str(other)), ("push", str(other)), ("unlink", str(other))):
            with self.subTest(args=args):
                self.assertIn("another branch's subplan", self.gdoc(*args, ok=False, cwd=docs).stderr)
        self.assertFalse([call for call in self.calls() if OTHER in call["params"].values()])

    def test_unsafe_link_records_are_refused(self) -> None:
        self.linked("spec.md", "text\n")
        for record in ({"path": "../outside.md", "document_id": DOC}, {"path": "gdoc-links.json", "document_id": DOC},
                       {"path": "/etc/spec.md", "document_id": DOC}, {"path": "spec.md", "document_id": "x -->"}):
            with self.subTest(record=record):
                (self.directory / "gdoc-links.json").write_text(json.dumps({"links": [record]}))
                self.assertIn("Malformed", self.gdoc("pull", ok=False).stderr)
                data = json.loads(self.cli("status", "--selector", "example", "--json").stdout)
                self.assertEqual(data["gdoc_links"], [])
        self.assertEqual(self.calls(), [])

    def test_failed_unlink_commit_explains_how_to_finish(self) -> None:
        path = self.linked("spec.md", "Text.\n\n" + block(FIRST, "image=kix.a"))
        lock = self.artifacts / ".git" / "index.lock"
        lock.write_text("")
        result = self.gdoc("unlink", str(path), ok=False)
        self.assertIn("written but not committed", result.stderr)
        self.assertEqual(path.read_text(), "Text.\n\n" + block(FIRST))
        lock.unlink()
        self.run_printed(result.stderr, "fix the cause shown above, then commit it with: ")
        self.assertEqual(self.git(self.artifacts, "status", "--porcelain"), "")
        self.assertFalse((self.directory / "gdoc-links.json").exists())

    # tools and status

    def test_missing_gws_fails_clearly_without_partial_writes(self) -> None:
        path = self.directory / "spec.md"
        path.write_text(block(FIRST))
        self.env["GWS_BIN"] = str(self.root / "missing" / "gws")
        before = self.commits()
        for args in (("link", str(path), DOC), ("link", str(path))):
            with self.subTest(args=args):
                result = self.gdoc(*args, ok=False)
                self.assertIn("gws not found", result.stderr)
                self.assertEqual(path.read_text(), block(FIRST))
                self.assertFalse((self.directory / "gdoc-links.json").exists())
        linked = self.linked("linked.md", "text\n")
        self.assertIn("gws not found", self.gdoc("pull", ok=False).stderr)
        self.assertEqual(linked.read_text(), header(DOC) + "\n\ntext\n")
        self.assertEqual(self.commits(), before + 1)

    def test_failed_gws_call_leaves_no_partial_writes(self) -> None:
        path = self.directory / "spec.md"
        path.write_text(block(FIRST))
        self.respond("docs documents get", {"exit": 1, "stdout": {"error": {"code": 401}}}, target=DOC)
        result = self.gdoc("link", str(path), DOC, ok=False)
        self.assertIn("gws auth login", result.stderr)
        self.assertNotIn("keyring", result.stderr)
        self.assertEqual(path.read_text(), block(FIRST))
        self.assertFalse((self.directory / "gdoc-links.json").exists())

    def test_status_lists_links_and_tolerates_a_malformed_state_file(self) -> None:
        self.linked("spec.md", "text\n")
        data = json.loads(self.cli("status", "--selector", "example", "--json").stdout)
        self.assertEqual(data["gdoc_links"], [{"path": str(self.directory / "spec.md"),
                                               "url": f"https://docs.google.com/document/d/{DOC}/edit"}])
        for malformed in ("{", '{"links": [{"path": 1, "document_id": "x"}]}', '{"links": 3}', "[]"):
            with self.subTest(malformed=malformed):
                (self.directory / "gdoc-links.json").write_text(malformed)
                data = json.loads(self.cli("status", "--selector", "example", "--json").stdout)
                self.assertEqual(data["gdoc_links"], [])
                self.assertEqual(data["errors"], [])


FAKE_GWS = r'''
import json, os, subprocess, sys
from pathlib import Path

state_path = Path(STATE)
args = sys.argv[1:]
words = []
while args and not args[0].startswith("-"):
    words.append(args.pop(0))
options = dict(zip(args[::2], args[1::2]))
params = json.loads(options.get("--params", "{}"))
body = json.loads(options["--json"]) if "--json" in options else None
command = " ".join(words)
if command == "docs documents get" and params.get("fields") == "revisionId":
    command = "revision"
for flag in ("--upload", "-o"):
    if flag in options and (os.path.isabs(options[flag]) or ".." in Path(options[flag]).parts):
        print("path outside the working directory", file=sys.stderr)
        sys.exit(2)
state = json.loads(state_path.read_text())
entry = {"command": command, "params": params, "body": body}
if "--upload" in options:
    data = Path(options["--upload"]).read_bytes()
    entry["upload_type"] = options["--upload-content-type"]
    entry["upload"] = data.decode() if entry["upload_type"].startswith("text/") else len(data)
state["log"].append(entry)
target = params.get("documentId") or params.get("fileId")
queue = state["responses"].get(f"{command} {target}") or state["responses"].get(command)
if not queue:
    state_path.write_text(json.dumps(state))
    print(f"unexpected call: {command} {target}", file=sys.stderr)
    sys.exit(3)
response = queue.pop(0) if len(queue) > 1 else queue[0]
state_path.write_text(json.dumps(state))
for path, content in response.get("write", {}).items():
    Path(path).write_text(content)
for command_line in response.get("run", []):
    subprocess.run(command_line, check=True, capture_output=True)
if "-o" in options:
    Path(options["-o"]).write_text(response.get("file", ""))
print("Using keyring backend: fake", file=sys.stderr)
if "stdout" in response:
    print(json.dumps(response["stdout"]))
sys.exit(response.get("exit", 0))
'''

FAKE_MMDC = r'''
import json, struct, sys, zlib
from pathlib import Path

args = sys.argv[1:]
source = Path(args[args.index("-i") + 1]).read_text()
log = json.loads(Path(LOG).read_text())
log.append(source)
Path(LOG).write_text(json.dumps(log))
if Path(FAIL).exists():
    print("Parse error on line 1", file=sys.stderr)
    sys.exit(1)


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


width, height = 400, 200
pixels = zlib.compress((b"\x00" + b"\xff\xff\xff" * width) * height)
Path(args[args.index("-o") + 1]).write_bytes(
    b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    + chunk(b"IDAT", pixels) + chunk(b"IEND", b""))
'''


if __name__ == "__main__":
    unittest.main()
