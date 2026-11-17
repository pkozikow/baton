"""Link Markdown project artifacts to Google Docs (`baton gdoc`).

The Doc is the source of truth for text: `pull` archives it into the Markdown file
and commits it. Mermaid source stays in the Markdown and `push` renders it into the
Doc as images. Imported only when `baton gdoc` runs; Google calls go through the
`gws` CLI and rendering through `mmdc`, so the rest of Baton needs neither.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
from typing import Any, Callable

from .doctor_freshness import require_trusted_runtime
from .errors import ProjectError
from .gitutil import git, inside, write_json


LINKS_FILE = "gdoc-links.json"
DOC_MIME = "application/vnd.google-apps.document"
PLACEHOLDER_PREFIX = "baton-gdoc-diagram-"
HEADER = re.compile(r"<!-- baton-gdoc: archive of .*-->")
MARK = re.compile(r"<!-- baton-gdoc (?:image=([\w.-]+)(?: sha256=([0-9a-f]{64}))?|placeholder=([a-z0-9-]+)) -->")
FENCE = re.compile(r"( {0,3})(`{3,}|~{3,})(.*)")
PLACEHOLDER = re.compile(re.escape(PLACEHOLDER_PREFIX) + r"([a-z0-9-]+)")
IMAGE_REFERENCE = re.compile(r"!\[[^\]]*\]\[(image\d+)\]")
IMAGE_DEFINITION = re.compile(r"\[image\d+\]: <data:[^>]*>")
DOC_URL = re.compile(r"https://docs\.google\.com/document/(?:u/\d+/)?d/([\w-]+)(?:[/?#].*)?")
BATCH_TIMEOUT = 60
RENDER_SCALE = 2
GWS_HINT = "install the Google Workspace CLI (gws) and sign in with `gws auth login`"
MMDC_HINT = "install the Mermaid CLI (`npm install -g @mermaid-js/mermaid-cli`)"


def doc_url(document_id: str) -> str:
    return f"https://docs.google.com/document/d/{document_id}/edit"


def header(document_id: str) -> str:
    return (f"<!-- baton-gdoc: archive of the Google Doc at {doc_url(document_id)} (edit the Doc, not this file; "
            "baton gdoc pull overwrites it) -->")


def mark_line(*, image: str | None = None, sha256: str | None = None, placeholder: str | None = None) -> str:
    if placeholder is not None:
        return f"<!-- baton-gdoc placeholder={placeholder} -->"
    return f"<!-- baton-gdoc image={image}" + (f" sha256={sha256}" if sha256 else "") + " -->"


# Markdown


@dataclass
class Block:
    """A Mermaid fenced block; `first` is its mark's line when it has one, otherwise its fence."""
    first: int
    fence: int
    end: int
    source: str
    image: str | None = None
    sha256: str | None = None
    placeholder: str | None = None

    @property
    def marked(self) -> bool:
        return self.image is not None or self.placeholder is not None

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.source.encode()).hexdigest()

    @property
    def stale(self) -> bool:
        return self.image is not None and self.sha256 != self.digest

    def describe(self) -> str:
        if self.placeholder is not None:
            return f"line {self.fence + 1} (placeholder={self.placeholder})"
        return f"line {self.fence + 1}" + (f" (image={self.image})" if self.image else "")

    def text(self, lines: list[str]) -> str:
        text = "".join(lines[self.first:self.end])
        return text if text.endswith("\n") else text + "\n"


def fenced_blocks(lines: list[str]) -> list[tuple[int, int, str, bool]]:
    """(opening line, line after the block, first info word, closed) for each fenced code block."""
    result = []
    opening: tuple[int, str, str] | None = None
    for number, line in enumerate(lines):
        text = line.rstrip("\r\n")
        if opening is None:
            match = FENCE.fullmatch(text)
            if match is None or (match[2][0] == "`" and "`" in match[3]):
                continue
            words = match[3].split()
            opening = (number, match[2], words[0].lower() if words else "")
        elif re.fullmatch(r" {0,3}" + re.escape(opening[1][0]) + "{%d,}[ \t]*" % len(opening[1]), text):
            result.append((opening[0], number + 1, opening[2], True))
            opening = None
    if opening is not None:
        result.append((opening[0], len(lines), opening[2], False))
    return result


def parse_blocks(lines: list[str]) -> list[Block]:
    blocks = []
    for start, end, language, closed in fenced_blocks(lines):
        if language != "mermaid":
            continue
        if not closed:
            raise ProjectError(f"Unclosed Mermaid fence at line {start + 1}.")
        # The line before an opening fence can never be inside another fence.
        mark = MARK.fullmatch(lines[start - 1].strip()) if start else None
        blocks.append(Block(first=start - 1 if mark else start, fence=start, end=end,
                            source="".join(lines[start + 1:end - 1]).replace("\r\n", "\n"),
                            image=mark[1] if mark else None, sha256=mark[2] if mark else None,
                            placeholder=mark[3] if mark else None))
    return blocks


def require_marked(blocks: list[Block]) -> None:
    unmarked = [block.describe() for block in blocks if not block.marked]
    if unmarked:
        raise ProjectError(f"Unmarked Mermaid block at {', '.join(unmarked)}. In a linked file every Mermaid block "
                           "needs a mark on the line before its fence: `<!-- baton-gdoc image=ID -->` for a diagram "
                           "already in the Doc, or `<!-- baton-gdoc placeholder=NAME -->` for one that push adds.")
    for kind in ("image", "placeholder"):
        values = [getattr(block, kind) for block in blocks if getattr(block, kind) is not None]
        repeated = sorted({value for value in values if values.count(value) > 1})
        if repeated:
            raise ProjectError(f"Several Mermaid blocks are marked {kind}={', '.join(repeated)}; each mark must be unique.")


def strip_header(lines: list[str]) -> list[str]:
    if lines and HEADER.fullmatch(lines[0].strip()):
        lines = lines[1:]
        if lines and not lines[0].strip():
            lines = lines[1:]
    return lines


def strip_marks(text: str) -> list[str]:
    lines = strip_header(text.splitlines(keepends=True))
    marks = {block.first for block in parse_blocks(lines) if block.marked}
    return [line for number, line in enumerate(lines) if number not in marks]


def with_header(document_id: str, body: str) -> str:
    return header(document_id) + "\n\n" + body


# Doc structure


@dataclass
class DocImage:
    object_id: str
    index: int
    standalone: bool
    before: str


@dataclass
class Placeholder:
    start: int
    end: int


@dataclass
class Structure:
    images: list[DocImage]
    placeholders: dict[str, list[Placeholder]]
    suggestions: int
    tab_id: str | None
    text_width: float


def readable(text: str) -> str:
    # Docs marks code blocks with private-use characters.
    text = "".join(character for character in text if not 0xE000 <= ord(character) <= 0xF8FF)
    return re.sub(r"\s+", " ", text).strip()


def read_structure(document: dict[str, Any]) -> Structure:
    try:
        return _read_structure(document)
    except (KeyError, TypeError, AttributeError) as error:
        raise ProjectError(f"The Doc's structure has an unexpected shape ({type(error).__name__}: {error}).") from error


def _read_structure(document: dict[str, Any]) -> Structure:
    tabs = document.get("tabs") or []
    if len(tabs) != 1 or tabs[0].get("childTabs"):
        raise ProjectError("The Doc has several tabs; baton gdoc supports only single-tab Docs.")
    tab = tabs[0]
    document_tab = tab.get("documentTab", {})
    images: list[DocImage] = []
    placeholders: dict[str, list[Placeholder]] = {}
    recent = ""

    def walk(content: list[dict[str, Any]], top_level: bool) -> None:
        nonlocal recent
        for item in content:
            paragraph = item.get("paragraph")
            if paragraph is not None:
                elements = paragraph.get("elements", [])
                style = paragraph.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
                # A fenced block can replace only a whole ordinary paragraph outside tables and lists.
                top = top_level and "bullet" not in paragraph and style == "NORMAL_TEXT" and all(
                    "textRun" in element or "inlineObjectElement" in element for element in elements)
                objects = [element for element in elements if "inlineObjectElement" in element]
                text = "".join(element.get("textRun", {}).get("content", "") for element in elements)
                for element in elements:
                    if "inlineObjectElement" in element:
                        images.append(DocImage(element["inlineObjectElement"]["inlineObjectId"], element["startIndex"],
                                               top and len(objects) == 1 and not text.strip(), readable(recent)[-60:].lstrip()))
                    recent = (recent + element.get("textRun", {}).get("content", ""))[-500:]
                match = PLACEHOLDER.fullmatch(text.strip().lower())
                if top and match and not objects and elements:
                    placeholders.setdefault(match[1], []).append(
                        Placeholder(elements[0]["startIndex"], elements[-1]["endIndex"] - 1))
            for row in item.get("table", {}).get("tableRows", []):
                for cell in row.get("tableCells", []):
                    walk(cell.get("content", []), False)
            walk(item.get("tableOfContents", {}).get("content", []), False)

    walk(document_tab.get("body", {}).get("content", []), True)
    style = document_tab.get("documentStyle", {})
    width = (style.get("pageSize", {}).get("width", {}).get("magnitude", 612)
             - style.get("marginLeft", {}).get("magnitude", 72) - style.get("marginRight", {}).get("magnitude", 72))
    return Structure(images, placeholders, len(suggestion_ids(document_tab.get("body", {}))),
                     tab.get("tabProperties", {}).get("tabId"), width)


def suggestion_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("suggestedInsertionIds", "suggestedDeletionIds") and isinstance(child, list):
                found.update(item for item in child if isinstance(item, str))
            else:
                found |= suggestion_ids(child)
    elif isinstance(value, list):
        for child in value:
            found |= suggestion_ids(child)
    return found


def image_listing(images: list[DocImage], named: set[str]) -> str:
    unmarked = [image for image in images if image.object_id not in named]
    lines = [f"  {image.object_id} after \"{image.before}\"" for image in unmarked] or ["  (none)"]
    return "Unmarked images in the Doc, in order:\n" + "\n".join(lines)


def refusal(problems: list[str], images: list[DocImage], named: set[str]) -> ProjectError:
    return ProjectError("\n".join(problems) + "\n" + image_listing(images, named))


# Transforms


def pull_text(current: str, export: str, structure: Structure, document_id: str) -> tuple[str, list[str]]:
    """Return the new archive for this export and notes on dropped or unarchived content."""
    lines = current.splitlines(keepends=True)
    blocks = parse_blocks(lines)
    require_marked(blocks)
    by_image = {block.image: block for block in blocks if block.image is not None}
    by_name = {block.placeholder: block for block in blocks if block.placeholder is not None}
    named = set(by_image)
    doc_ids = [image.object_id for image in structure.images]
    unmarked = [image for image in structure.images if image.object_id not in named]
    raw = export.splitlines(keepends=True)
    raw_code = {number for start, end, _language, _closed in fenced_blocks(raw) for number in range(start, end)}
    kept = "".join(line for number, line in enumerate(raw)
                   if number in raw_code or not IMAGE_DEFINITION.fullmatch(line.strip()))
    exported = (kept.rstrip("\n") + "\n").splitlines(keepends=True)
    code: set[int] = set()
    mermaid_fences: set[int] = set()
    for start, end, language, _closed in fenced_blocks(exported):
        code.update(range(start, end))
        if language == "mermaid":
            mermaid_fences.add(start)
    references = [(number, match) for number, line in enumerate(exported) if number not in code
                  for match in IMAGE_REFERENCE.finditer(line)]
    if len(references) != len(structure.images):
        raise refusal([f"The export has {len(references)} image references but the Doc has {len(structure.images)} "
                       "inline images; a text-wrapped image is the likely cause. Set its wrapping to \"In line\" in "
                       "the Doc and pull again."], structure.images, named)
    problems: list[str] = []
    notes: list[str] = []
    repeated = sorted({image_id for image_id in doc_ids if doc_ids.count(image_id) > 1})
    if repeated:
        problems.append(f"Images {', '.join(repeated)} appear more than once in the Doc.")
    paired: dict[int, list[tuple[re.Match[str], DocImage]]] = {}
    for (number, match), image in zip(references, structure.images):
        paired.setdefault(number, []).append((match, image))
    for image_id, block in by_image.items():
        if image_id in doc_ids:
            continue
        if unmarked:
            problems.append(f"The image of the block at {block.describe()} is no longer in the Doc, and the Doc has "
                            "unmarked images, so it may have been cut and pasted. Put the right ID in the block's mark, "
                            "or delete the block and commit, then pull again.")
        else:
            notes.append(f"Dropped the Mermaid block at {block.describe()}: its image was deleted from the Doc.")
    placeholder_at: dict[int, str] = {}
    for number, line in enumerate(exported):
        match = PLACEHOLDER.fullmatch(line.strip().lower())
        if number not in code and match and match[1] in by_name:
            placeholder_at[number] = match[1]
    for name, block in by_name.items():
        count = len(structure.placeholders.get(name, []))
        if count != 1:
            where = "is missing from the Doc" if not count else f"appears {count} times in the Doc"
            problems.append(f"The placeholder paragraph {PLACEHOLDER_PREFIX}{name} for the block at {block.describe()} "
                            f"{where}. Add or fix it in the Doc as its own paragraph, or delete the block and commit. "
                            "If a push was interrupted the image may already be in the Doc: change the mark to "
                            "image=ID with an ID listed below, and commit.")
        elif list(placeholder_at.values()).count(name) != 1:
            problems.append(f"The placeholder {PLACEHOLDER_PREFIX}{name} is not on a line of its own in the export; "
                            "remove any formatting from that paragraph in the Doc and pull again.")
    output: list[str] = []
    for number, line in enumerate(exported):
        if number in mermaid_fences:
            match = FENCE.fullmatch(line.rstrip("\r\n"))
            assert match is not None
            # Unlabelled, a code block from the Doc can never be taken for a diagram to restore or render.
            output.append(match[1] + match[2] + "\n")
            notes.append(f"Removed the mermaid label from a code block exported from the Doc (line {number + 1}).")
        elif number in code:
            output.append(line)
        elif number in paired:
            items = paired[number]
            marked = [image for _match, image in items if image.object_id in by_image]
            if marked:
                if len(items) == 1 and line.strip() == items[0][0][0] and marked[0].standalone:
                    output.append(by_image[marked[0].object_id].text(lines))
                else:
                    problems.append(f"The image of the block at {by_image[marked[0].object_id].describe()} is not "
                                    "alone in its own paragraph (it is inline in a sentence, in a table or in a list), "
                                    "where a fenced block would corrupt the Markdown. Move the image to its own "
                                    "paragraph in the Doc, or delete the block and commit.")
                continue
            for match, image in reversed(items):
                line = line[:match.start()] + f"*(image {image.object_id} not archived)*" + line[match.end():]
                notes.append(f"Image {image.object_id} is not a diagram from this file; archived as a note.")
            output.append(line)
        else:
            output.append(by_name[placeholder_at[number]].text(lines) if number in placeholder_at else line)
    if problems:
        raise refusal(problems, structure.images, named)
    if structure.suggestions:
        notes.append(f"The Doc has {structure.suggestions} pending suggestion(s); suggested insertions are archived "
                     "as ordinary text and text suggested for deletion is still archived.")
    return with_header(document_id, "".join(output)), notes


def adopt_text(current: str, structure: Structure, document_id: str) -> str:
    """Mark unmarked Mermaid blocks with the Doc's unnamed images in order and add the archive header."""
    lines = strip_header(current.splitlines(keepends=True))
    blocks = parse_blocks(lines)
    marked = [block for block in blocks if block.marked]
    require_marked(marked)
    named = {block.image for block in marked if block.image is not None}
    doc_ids = {image.object_id for image in structure.images}
    missing = [block.describe() for block in marked if block.image is not None and block.image not in doc_ids]
    if missing:
        raise refusal([f"Blocks at {', '.join(missing)} name images that are not in the Doc."], structure.images, named)
    unmarked = [block for block in blocks if not block.marked]
    free = [image for image in structure.images if image.object_id not in named]
    if unmarked and len(unmarked) != len(free):
        raise refusal([f"The Markdown has {len(unmarked)} unmarked Mermaid blocks but the Doc has {len(free)} images "
                       "that no mark names, so they cannot be paired in order. Mark the blocks by hand first "
                       "(`<!-- baton-gdoc image=ID -->` on the line before each fence), then link again."],
                      structure.images, named)
    for block, image in reversed(list(zip(unmarked, free))):
        lines.insert(block.fence, mark_line(image=image.object_id, sha256=block.digest) + "\n")
    return with_header(document_id, "".join(lines))


def remark(text: str, marks: dict[int, str]) -> str:
    """Replace the mark lines at these line numbers, keeping each line's ending."""
    lines = text.splitlines(keepends=True)
    for number, mark in marks.items():
        lines[number] = mark + lines[number][len(lines[number].rstrip("\r\n")):]
    return "".join(lines)


def document_id_from(value: str) -> str:
    match = DOC_URL.fullmatch(value.strip())
    if match:
        return match[1]
    if re.fullmatch(r"[\w-]{20,}", value.strip()):
        return value.strip()
    raise ProjectError(f"Expected a Google Docs URL (https://docs.google.com/document/d/ID/...) or ID: {value}")


# External tools


def tool(name: str, variable: str, hint: str) -> str:
    found = shutil.which(os.environ.get(variable) or name)
    if not found:
        raise ProjectError(f"{name} not found (set {variable} or put it on PATH); {hint}.")
    # Commands run from a temporary directory, where a relative path would no longer resolve.
    return str(Path(found).absolute())


def gws(command: str, params: dict[str, Any], body: dict[str, Any] | None = None, *, upload: bytes | None = None,
        upload_type: str | None = None, download: bool = False, timeout: int = 180) -> Any:
    """Make one Google API call; every Google call goes through here so the transport can be replaced."""
    arguments = [tool("gws", "GWS_BIN", GWS_HINT), *command.split(), "--params", json.dumps(params)]
    if body is not None:
        arguments += ["--json", json.dumps(body)]
    with tempfile.TemporaryDirectory(prefix="baton-gdoc-") as directory:
        # gws reads uploads and writes downloads only inside its working directory.
        if upload is not None:
            name = "upload" + {"text/markdown": ".md", "image/png": ".png"}.get(upload_type or "", "")
            (Path(directory) / name).write_bytes(upload)
            arguments += ["--upload", name, "--upload-content-type", upload_type or "application/octet-stream"]
        if download:
            arguments += ["-o", "download"]
        try:
            result = subprocess.run(arguments, cwd=directory, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise ProjectError(f"gws {command} timed out after {timeout}s.") from error
        if result.returncode:
            detail = "\n".join(line for line in (result.stderr + "\n" + result.stdout).strip().splitlines()
                               if not line.startswith("Using keyring backend"))[-2000:]
            hint = ("\nCheck `gws auth status`; if needed run `gws auth login` with Docs and Drive access."
                    if re.search(r"auth|401|403|insufficient|credential", detail, re.IGNORECASE) else "")
            raise ProjectError(f"gws {command} failed (exit {result.returncode}): {detail or 'no output'}{hint}")
        if download:
            target = Path(directory) / "download"
            if not target.is_file():
                raise ProjectError(f"gws {command} wrote no output file.")
            return target.read_bytes().decode()
    if not result.stdout.strip():
        return {}
    try:
        response = json.loads(result.stdout)
    except ValueError as error:
        raise ProjectError(f"gws {command} printed output that is not JSON: {result.stdout[:200]}") from error
    if not isinstance(response, dict):
        raise ProjectError(f"gws {command} printed an unexpected response: {result.stdout[:200]}")
    return response


def get_document(document_id: str) -> dict[str, Any]:
    return gws("docs documents get", {"documentId": document_id, "includeTabsContent": True})


def revision_of(document: dict[str, Any]) -> str:
    value = document.get("revisionId")
    if not isinstance(value, str) or not value:
        raise ProjectError("The Doc returned no revisionId; baton gdoc needs edit access to it.")
    return value


def revision(document_id: str) -> str:
    return revision_of(gws("docs documents get", {"documentId": document_id, "fields": "revisionId"}))


def create_doc(name: str, content: bytes, content_type: str) -> str:
    """Import a file into a new Google Doc and return its ID."""
    created = gws("drive files create", {"fields": "id"}, {"name": name, "mimeType": DOC_MIME},
                  upload=content, upload_type=content_type)
    if not isinstance(created.get("id"), str) or not created["id"]:
        raise ProjectError("gws drive files create returned no file ID.")
    return str(created["id"])


def render(block: Block) -> bytes:
    with tempfile.TemporaryDirectory(prefix="baton-gdoc-render-") as directory:
        source, image = Path(directory) / "diagram.mmd", Path(directory) / "diagram.png"
        source.write_text(block.source)
        try:
            result = subprocess.run([tool("mmdc", "MMDC_BIN", MMDC_HINT), "-i", str(source), "-o", str(image),
                                     "-w", "1600", "-s", str(RENDER_SCALE), "-q"], capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired as error:
            raise ProjectError(f"mmdc timed out rendering the block at {block.describe()}.") from error
        if result.returncode or not image.is_file():
            raise ProjectError(f"mmdc failed to render the block at {block.describe()}: "
                               f"{(result.stderr or result.stdout).strip()[-2000:]}")
        data = image.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) < 24:
        raise ProjectError(f"mmdc did not produce a PNG for the block at {block.describe()}.")
    if len(data) > 20 * 1024 * 1024:
        raise ProjectError(f"The diagram at {block.describe()} exceeds Google Docs' 20 MB image limit.")
    return data


def image_size(png: bytes, text_width: float) -> dict[str, Any]:
    width, height = struct.unpack(">II", png[16:24])
    if not width or not height or width * height > 25_000_000:
        raise ProjectError("A rendered diagram exceeds Google Docs' 25 megapixel image limit.")
    # A CSS pixel is 0.75 pt, and mmdc renders RENDER_SCALE device pixels per CSS pixel.
    scale = min(text_width / width, 0.75 / RENDER_SCALE)
    return {"width": {"magnitude": width * scale, "unit": "PT"}, "height": {"magnitude": height * scale, "unit": "PT"}}


def image_uri(png: bytes, temporary: list[str]) -> str:
    # A PNG imported as a private Doc yields a short-lived image URI without sharing anything publicly.
    temporary.append(create_doc("baton gdoc temporary image (safe to delete)", png, "image/png"))
    document = get_document(temporary[-1])
    uris = [item.get("inlineObjectProperties", {}).get("embeddedObject", {}).get("imageProperties", {}).get("contentUri")
            for tab in document.get("tabs", []) for item in tab.get("documentTab", {}).get("inlineObjects", {}).values()]
    uris = [uri for uri in uris if isinstance(uri, str)]
    if len(uris) != 1:
        raise ProjectError("Google's PNG import did not produce exactly one image.")
    return uris[0]


# Commands


@dataclass
class Target:
    block: Block
    start: int
    end: int


class Gdoc:
    def __init__(self, manager: Any, project: dict[str, Any]) -> None:
        self.manager = manager
        directory = manager.artifact_directory(project)
        if directory.is_symlink() or not directory.is_dir():
            raise ProjectError(f"Unsafe or missing project artifacts directory: {directory}")
        self.directory = directory.resolve()
        self.root = manager.artifacts_git(directory)
        self.links_path = self.directory / LINKS_FILE
        owned = manager.owned_artifacts(project, directory)
        self.owned = owned.resolve() if owned is not None else None

    def foreign(self, path: Path) -> bool:
        """Another branch's subplan, which baton artifacts commit would also refuse from this checkout."""
        return (self.owned is not None and inside(path, self.directory / "branches")
                and not inside(path, self.owned))

    def artifact(self, supplied: str) -> Path:
        path = Path(os.path.abspath(supplied))
        if path.is_symlink():
            raise ProjectError(f"Refusing a symlinked artifact: {supplied}")
        resolved = path.resolve()
        if not inside(resolved, self.directory):
            raise ProjectError(f"Artifact path is outside {self.directory}: {supplied}")
        if resolved.suffix != ".md" or not resolved.is_file():
            raise ProjectError(f"Not a Markdown file: {supplied}")
        if self.foreign(resolved):
            raise ProjectError(f"{supplied} is another branch's subplan; from this branch checkout only {self.owned} "
                               "is yours. Leave other subplans to their workers or the parent.")
        return resolved

    def name(self, path: Path) -> str:
        return path.relative_to(self.directory).as_posix()

    def relative(self, path: Path) -> str:
        return str(path.relative_to(self.root))

    def read_links(self) -> list[dict[str, str]]:
        if self.links_path.is_symlink():
            raise ProjectError(f"Refusing a symlinked {self.links_path}")
        if not self.links_path.exists():
            return []
        try:
            links = json.loads(self.links_path.read_text())["links"]
            # Records come from Git, possibly another machine's commit: hold them to the command-line path rules.
            if not isinstance(links, list) or not all(
                    isinstance(link, dict) and isinstance(link.get("path"), str)
                    and isinstance(link.get("document_id"), str) and re.fullmatch(r"[\w-]+", link["document_id"])
                    and not PurePosixPath(link["path"]).is_absolute() and ".." not in PurePosixPath(link["path"]).parts
                    and link["path"].endswith(".md") for link in links):
                raise TypeError
        except (ValueError, KeyError, TypeError) as error:
            raise ProjectError(f"Malformed {self.links_path}; fix or restore it from Git.") from error
        return [{"path": link["path"], "document_id": link["document_id"]} for link in links]

    def write_links(self, links: list[dict[str, str]]) -> None:
        if links:
            write_json(self.links_path, {"links": links})
        else:
            self.links_path.unlink(missing_ok=True)

    def link_for(self, path: Path) -> dict[str, str]:
        found = [link for link in self.read_links() if link["path"] == self.name(path)]
        if not found:
            raise ProjectError(f"{self.name(path)} is not linked to a Google Doc; see baton gdoc link.")
        return found[0]

    def clean(self, path: Path) -> bool:
        tracked = bool(git(self.root, "--literal-pathspecs", "ls-files", "--", self.relative(path)))
        return tracked and subprocess.run(["git", "-C", str(self.root), "--literal-pathspecs", "diff", "--quiet",
                                           "HEAD", "--", self.relative(path)], capture_output=True).returncode == 0

    def require_clean(self, path: Path) -> None:
        if not self.clean(path):
            raise ProjectError(f"{self.name(path)} is untracked or has uncommitted changes; commit it first "
                               "(baton artifacts commit).")

    @staticmethod
    def read(path: Path) -> str:
        if path.is_symlink():
            raise ProjectError(f"Refusing a symlinked artifact: {path}")
        return path.read_bytes().decode()

    def commit(self, message: str, paths: list[Path], prepare: Callable[[], None], push: bool = True) -> None:
        self.manager.commit_artifact_paths(self.root, message, [self.relative(path) for path in paths], push=push,
                                           artifacts_root=self.directory.parent, prepare=prepare)

    def save(self, path: Path, original: str, new: str, update: Callable[[list[dict[str, str]]], list[dict[str, str]]],
             message: str) -> None:
        def prepare() -> None:
            # Under the artifact lock, after the checkout synced: nothing may have changed since it was read.
            if self.read(path) != original:
                raise ProjectError(f"{self.name(path)} changed while the command ran; nothing was recorded. Run it again.")
            links = update(self.read_links())
            path.write_bytes(new.encode())
            self.write_links(links)
        try:
            self.commit(message, [path, self.links_path], prepare)
        except ProjectError as error:
            if self.read(path) == new != original:
                raise ProjectError(f"{error}\nThe change is written but not committed; commit it with: "
                                   f"{shlex.join(['baton', 'artifacts', 'commit', '-m', message, str(path), str(self.links_path)])}"
                                   ) from error
            raise

    def link(self, supplied: str, url: str | None, title: str | None) -> None:
        path = self.artifact(supplied)
        name = self.name(path)
        if any(link["path"] == name for link in self.read_links()):
            raise ProjectError(f"{name} is already linked; unlink it first.")
        original = self.read(path)

        def add(document_id: str) -> Callable[[list[dict[str, str]]], list[dict[str, str]]]:
            def update(links: list[dict[str, str]]) -> list[dict[str, str]]:
                for link in links:
                    if link["path"] == name or link["document_id"] == document_id:
                        raise ProjectError(f"{link['path']} is already linked to {doc_url(link['document_id'])}.")
                return [*links, {"path": name, "document_id": document_id}]
            return update

        if url is not None:
            document_id = document_id_from(url)
            add(document_id)(self.read_links())
            new = adopt_text(original, read_structure(get_document(document_id)), document_id)
            self.save(path, original, new, add(document_id), f"Link {name} to its Google Doc")
            print(f"Linked {name} to {doc_url(document_id)}")
            return
        lines = strip_marks(original)
        blocks = parse_blocks(lines)
        if blocks:
            tool("mmdc", "MMDC_BIN", MMDC_HINT)
        imported = list(lines)
        for number, block in reversed(list(enumerate(blocks, 1))):
            imported[block.fence:block.end] = [f"\n{PLACEHOLDER_PREFIX}{number}\n\n"]
            lines.insert(block.fence, mark_line(placeholder=str(number)) + "\n")
        code = {number for start, end, _language, _closed in fenced_blocks(lines) for number in range(start, end)}
        heading = next((line[2:].strip() for number, line in enumerate(lines)
                        if number not in code and line.startswith("# ")), "")
        document_id = create_doc(title or heading or path.stem, "".join(imported).encode(), "text/markdown")
        print(f"Created {doc_url(document_id)}", flush=True)
        try:
            self.save(path, original, with_header(document_id, "".join(lines)), add(document_id),
                      f"Link {name} to a new Google Doc")
        except ProjectError as error:
            if any(link["document_id"] == document_id for link in self.read_links()):
                raise ProjectError(f"{error}\nThen run: {shlex.join(['baton', 'gdoc', 'push', str(path)])}") from error
            adopt = shlex.join(["baton", "gdoc", "link", str(path), doc_url(document_id)])
            # The new Doc holds placeholder paragraphs, not images, so adoption needs the matching pending marks.
            marks = (f" First mark its Mermaid blocks, in order, placeholder=1 to placeholder={len(blocks)} "
                     "(`<!-- baton-gdoc placeholder=1 -->` on the line before each fence); after linking, push."
                     if blocks else "")
            raise ProjectError(f"{error}\nThe Doc was created but not linked.{marks} Adopt it with: {adopt}") from error
        print(f"Linked {name} to {doc_url(document_id)}", flush=True)
        if blocks:
            try:
                self.push(str(path), dry_run=False)
            except ProjectError as error:
                raise ProjectError(f"{error}\nThe link is recorded and its diagrams are pending; fix the cause and run: "
                                   f"{shlex.join(['baton', 'gdoc', 'push', str(path)])}") from error

    def unlink(self, supplied: str) -> None:
        path = self.artifact(supplied)
        name = self.name(path)
        self.link_for(path)
        original = self.read(path)

        def update(links: list[dict[str, str]]) -> list[dict[str, str]]:
            if not any(link["path"] == name for link in links):
                raise ProjectError(f"{name} is not linked to a Google Doc.")
            return [link for link in links if link["path"] != name]

        self.save(path, original, "".join(strip_marks(original)), update, f"Unlink {name} from its Google Doc")
        print(f"Unlinked {name}; the Google Doc is unchanged.")

    def pull(self, supplied: list[str], dry_run: bool, push: bool) -> int:
        paths = [self.artifact(path) for path in supplied]
        # Sync once for the whole run rather than once per committed link.
        remote = push and not dry_run and "origin" in git(self.root, "remote").splitlines()
        default = self.manager.artifact_checkout(self.root, []) if remote else ""
        fetched = remote and self.manager.fetch_artifacts(self.root, default)
        if fetched:
            with self.manager.artifacts_lock(self.root):
                self.manager.fast_forward_artifacts(self.root, default, artifacts_root=self.directory.parent)
        if paths:
            links = [self.link_for(path) for path in paths]
        else:
            links = []
            for link in self.read_links():
                if self.foreign(self.directory / link["path"]):
                    print(f"{link['path']}: skipped; it is another branch's subplan.")
                else:
                    links.append(link)
        if not links:
            print("No artifacts to pull are linked to a Google Doc.")
        failed = 0
        for link in links:
            try:
                self.pull_one(link, dry_run)
            except (ProjectError, OSError, ValueError) as error:
                failed += 1
                print(f"baton gdoc: {link['path']}: not pulled: {error}", file=sys.stderr, flush=True)
        if fetched:
            self.manager.push_artifacts(self.root, default, artifacts_root=self.directory.parent)
        elif remote:
            self.manager.artifact_warning(self.root, default, "Push skipped because fetch failed; pulled archives are "
                                                              "committed locally. Pull again to publish them.")
        return 1 if failed else 0

    def pull_one(self, link: dict[str, str], dry_run: bool) -> None:
        path = self.directory / link["path"]
        if not inside(path.resolve(), self.directory) or path.is_symlink() or not path.is_file():
            raise ProjectError(f"Linked artifact is missing or unsafe: {path}")
        document_id = link["document_id"]
        snapshot = self.read(path)
        for _attempt in range(2):
            before = revision(document_id)
            export = gws("drive files export", {"fileId": document_id, "mimeType": "text/markdown"}, download=True)
            document = get_document(document_id)
            # The structure's own revision, read after the export, brackets the export with the first read.
            if revision_of(document) == before:
                break
            print(f"baton gdoc: {link['path']}: the Doc changed during the export; retrying once.", file=sys.stderr)
        else:
            raise ProjectError("The Doc changed during the export twice; pull again when its edits pause.")
        new, notes = pull_text(snapshot, export, read_structure(document), document_id)
        for note in notes:
            print(f"{link['path']}: {note}")
        if dry_run:
            diff = list(difflib.unified_diff(snapshot.splitlines(keepends=True), new.splitlines(keepends=True),
                                             f"a/{link['path']}", f"b/{link['path']}"))
            added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
            removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
            print(f"{link['path']}: {added} lines added, {removed} removed (dry run; nothing written)")
            sys.stdout.writelines(line if line.endswith("\n") else line + "\n" for line in diff)
            return
        if new == snapshot and self.clean(path):
            print(f"{link['path']}: unchanged")
            return

        def prepare() -> None:
            current = self.read(path)
            if current == new:
                return
            if not self.clean(path):
                raise ProjectError("The Markdown is untracked or has uncommitted changes, which a pull would destroy; "
                                   "commit or discard them first.")
            if current != snapshot:
                raise ProjectError("The Markdown changed while the Doc was exported, perhaps by another pull; "
                                   "nothing was written. Pull again.")
            path.write_bytes(new.encode())
        self.manager.commit_artifact_paths(self.root, f"Pull {link['path']} from its Google Doc", [self.relative(path)],
                                           push=False, sync=False, artifacts_root=self.directory.parent, prepare=prepare)

    def require_current(self) -> None:
        """Push changes the Doc irreversibly, so unlike plain artifact commits it refuses a stale checkout."""
        if "origin" not in git(self.root, "remote").splitlines():
            return
        default = self.manager.artifact_checkout(self.root, [])
        if not self.manager.fetch_artifacts(self.root, default):
            raise ProjectError("Fetching the artifact checkout failed; push changes the Doc only from an up-to-date "
                               "checkout. Retry when the remote is reachable.")
        with self.manager.artifacts_lock(self.root):
            self.manager.fast_forward_artifacts(self.root, default, artifacts_root=self.directory.parent)
        self.require_not_behind()

    def require_not_behind(self) -> None:
        if "origin" not in git(self.root, "remote").splitlines():
            return
        default = self.manager.artifact_checkout(self.root, [])
        counts = self.manager.artifact_divergence(self.root, default)
        if counts and counts[1]:
            raise ProjectError(f"The artifact checkout {self.root} is {counts[1]} commit(s) behind origin/{default} "
                               "and cannot be fast-forwarded; nothing was sent to the Doc. Ask the user to reconcile it.")

    def push(self, supplied: str, dry_run: bool) -> None:
        path = self.artifact(supplied)
        name = self.name(path)
        self.link_for(path)
        blocks = parse_blocks(self.read(path).splitlines(keepends=True))
        require_marked(blocks)
        if not any(block.placeholder is not None or block.stale for block in blocks):
            print(f"{name}: no pending or changed diagrams to push.")
            return
        self.require_clean(path)
        if not dry_run:
            self.require_current()
        # The fast-forward may have changed the file or its link, so everything below works from these reads.
        document_id = self.link_for(path)["document_id"]
        original = self.read(path)
        blocks = parse_blocks(original.splitlines(keepends=True))
        require_marked(blocks)
        document = get_document(document_id)
        structure = read_structure(document)
        document_revision = revision_of(document)
        named = {block.image for block in blocks if block.image is not None}
        images = {image.object_id: image for image in structure.images}
        problems: list[str] = []
        targets: list[Target] = []
        for block in blocks:
            if block.image is not None and block.image not in images:
                problems.append(f"The image of the block at {block.describe()} is not in the Doc; pull first.")
            elif block.placeholder is not None:
                found = structure.placeholders.get(block.placeholder, [])
                if len(found) != 1:
                    problems.append(f"The placeholder paragraph {PLACEHOLDER_PREFIX}{block.placeholder} for the block "
                                    f"at {block.describe()} must be in the Doc exactly once as its own paragraph, "
                                    f"outside tables and lists; found {len(found)}.")
                else:
                    targets.append(Target(block, found[0].start, found[0].end))
            elif block.stale and block.image is not None:
                image = images[block.image]
                targets.append(Target(block, image.index, image.index + 1))
        if problems:
            raise refusal(problems, structure.images, named)
        if not targets:
            print(f"{name}: no pending or changed diagrams to push.")
            return
        for target in targets:
            print(f"{name}: {'would render' if dry_run else 'rendering'} the block at {target.block.describe()}",
                  flush=True)
        if dry_run:
            return
        pngs = [render(target.block) for target in targets]
        sizes = [image_size(png, structure.text_width) for png in pngs]
        temporary: list[str] = []
        state: dict[str, Any] = {"attempted": False, "ids": None}
        try:
            uris = [image_uri(png, temporary) for png in pngs]
            # Work from the end of the Doc so each change leaves earlier indices valid.
            ordered = sorted(zip(targets, uris, sizes), key=lambda item: item[0].start, reverse=True)
            requests: list[dict[str, Any]] = []
            for target, uri, size in ordered:
                where = {"tabId": structure.tab_id} if structure.tab_id else {}
                requests += [{"deleteContentRange": {"range": {"startIndex": target.start, "endIndex": target.end,
                                                                **where}}},
                             {"insertInlineImage": {"uri": uri, "location": {"index": target.start, **where},
                                                    "objectSize": size}}]

            def prepare() -> None:
                self.require_not_behind()
                if self.read(path) != original or self.link_for(path)["document_id"] != document_id:
                    raise ProjectError(f"{name} or its link changed during the push; nothing was sent to the Doc. "
                                       "Push again.")
                state["attempted"] = True
                reply = gws("docs documents batchUpdate", {"documentId": document_id},
                            {"requests": requests, "writeControl": {"requiredRevisionId": document_revision}},
                            timeout=BATCH_TIMEOUT)
                try:
                    ids = [reply["replies"][2 * position + 1]["insertInlineImage"]["objectId"]
                           for position in range(len(ordered))]
                    if not all(isinstance(image_id, str) and image_id for image_id in ids):
                        raise TypeError
                except (KeyError, IndexError, TypeError) as error:
                    raise ProjectError("gws docs documents batchUpdate returned no new image IDs.") from error
                state["ids"] = {target.block.first: image_id for (target, _uri, _size), image_id in zip(ordered, ids)}
                path.write_bytes(remark(original, {
                    target.block.first: mark_line(image=state["ids"][target.block.first], sha256=target.block.digest)
                    for target in targets}).encode())

            try:
                self.commit(f"Push diagrams of {name} to its Google Doc", [path], prepare)
            except (ProjectError, OSError) as error:
                if state["ids"] is not None:
                    listing = "\n".join(f"  {target.block.describe()} -> image={state['ids'][target.block.first]}"
                                        for target in targets)
                    raise ProjectError(f"{error}\nThe Doc was updated; the blocks' new marks are:\n{listing}\n"
                                       f"{name} normally holds them already (set them by hand if not, without sha256). "
                                       "Commit it with: " + shlex.join(["baton", "artifacts", "commit", "-m",
                                                                        f"Push diagrams of {name}", str(path)])
                                       ) from error
                if state["attempted"]:
                    raise ProjectError(f"{error}\n{self.interrupted(document_id, targets, named)}") from error
                raise
        finally:
            for file_id in temporary:
                try:
                    gws("drive files delete", {"fileId": file_id}, timeout=30)
                except ProjectError as error:
                    print(f"baton gdoc: warning: could not delete temporary Doc {file_id}: {error}", file=sys.stderr)
        for target in targets:
            print(f"{name}: the block at {target.block.describe()} is now image={state['ids'][target.block.first]}")

    @staticmethod
    def interrupted(document_id: str, targets: list[Target], named: set[str]) -> str:
        pushed = "\n".join(f"  {target.block.describe()}" for target in targets)
        advice = ("The Doc update may still have been applied. For each diagram below whose new image is listed, put "
                  "the ID in its block's mark as image=ID without sha256 (a placeholder mark becomes an image mark) and "
                  "commit; the next push re-renders it. If no new image is listed, push again.\n"
                  f"Diagrams in this push:\n{pushed}\n")
        try:
            return advice + image_listing(read_structure(get_document(document_id)).images, named)
        except ProjectError as error:
            return advice + f"Could not re-read the Doc to list its images: {error}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="baton gdoc", description="Link Markdown project artifacts to Google Docs. "
                                     "Requires the gws CLI, and mmdc to render Mermaid diagrams.")
    actions = parser.add_subparsers(dest="action", required=True)
    link = actions.add_parser("link", help="Adopt an existing Doc, or create one from the Markdown")
    link.add_argument("path", help="Markdown file inside the project's artifact directory")
    link.add_argument("url", nargs="?", metavar="DOC_URL", help="Existing Doc to adopt (default: create a new Doc)")
    link.add_argument("--title", help="Title of a new Doc (default: the first heading or the file name)")
    pull = actions.add_parser("pull", help="Archive linked Docs into their Markdown files and commit them")
    pull.add_argument("paths", nargs="*", metavar="PATH", help="Linked files (default: all of the project's links)")
    pull.add_argument("--dry-run", action="store_true", help="Print the diff and write nothing")
    pull.add_argument("--no-push", action="store_true", help="Commit locally without syncing with or pushing to origin")
    push = actions.add_parser("push", help="Render pending or changed Mermaid diagrams into the Doc")
    push.add_argument("path")
    push.add_argument("--dry-run", action="store_true", help="List the diagrams to render and change nothing")
    unlink = actions.add_parser("unlink", help="Forget a link and remove its header and marks; the Doc is unchanged")
    unlink.add_argument("path")
    for command in (link, pull, push, unlink):
        command.add_argument("--selector", help="Project UUID, name, or coordinator slot (default: current project)")
    args = parser.parse_args(argv)
    if args.selector is not None and not args.selector.strip():
        parser.error("Project selector must not be empty")
    if args.action == "link" and args.url is not None and args.title is not None:
        parser.error("--title applies only when creating a new Doc")
    try:
        if not getattr(args, "dry_run", False):
            require_trusted_runtime()
        from .manager import Manager
        manager = Manager()
        manager.load()
        gdoc = Gdoc(manager, manager.artifact_project(args.selector))
        if args.action == "link":
            gdoc.link(args.path, args.url, args.title)
        elif args.action == "pull":
            return gdoc.pull(args.paths, args.dry_run, push=not args.no_push)
        elif args.action == "push":
            gdoc.push(args.path, args.dry_run)
        else:
            gdoc.unlink(args.path)
        return 0
    except (ProjectError, OSError, ValueError) as error:
        print(f"baton gdoc: {error}", file=sys.stderr)
        return 1
