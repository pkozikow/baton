# Google Doc archives

`baton gdoc` links a Markdown file in a project's artifact directory to a Google
Doc. Once linked, the Doc is where people comment and edit, and the Markdown file
is its archive: `baton gdoc pull` exports the Doc into the file and commits it, so
Git keeps a diffable history of a document whose source of truth is the Doc.
Mermaid diagrams appear in the Doc as rendered images and stay as Mermaid source
in the Markdown.

Agents use the explicitly invoked [`baton-gdoc`](../agent-skills/baton-gdoc/SKILL.md)
skill, which states the workflow rules and how to recover from each refusal.

## Requirements

- `gws`, the Google Workspace CLI, signed in (`gws auth login`) with Docs and
  Drive access. Every Google call goes through it.
- `mmdc` from [Mermaid CLI](https://github.com/mermaid-js/mermaid-cli), only when
  a diagram must be rendered.

`GWS_BIN` and `MMDC_BIN` override the executables, as `<NAME>_BIN` does for
provider CLIs. A missing tool, missing authentication or a failed call stops the
command with a message naming the fix. Without these tools every other Baton
command behaves as before. There is nothing to configure.

## Commands

All commands take `--selector PROJECT`. Paths must be Markdown files inside the
project's artifact directory and not symlinks; relative paths resolve against the
current directory. As with `baton artifacts commit`, a branch checkout of a
multi-branch project may not act on another branch's subplan, and a pull of all
links skips them.

| Command | Does |
|---|---|
| `baton gdoc link PATH DOC_URL` | Adopt an existing Doc: mark the Mermaid blocks with the Doc's images, add the archive header, record the link and commit. Writes nothing to the Doc |
| `baton gdoc link PATH [--title T]` | Create a Doc from the Markdown (title defaults to the first `#` heading, then the file name), print its URL, record the link and commit, then push the diagrams |
| `baton gdoc pull [PATH...] [--dry-run] [--no-push]` | Export each linked Doc (all of the project's links by default), restore the Mermaid blocks, write the file and commit, one commit per changed file. `--dry-run` prints the diff and writes nothing |
| `baton gdoc push PATH [--dry-run]` | Render Mermaid blocks that are pending or whose source changed, and replace only those images in the Doc |
| `baton gdoc unlink PATH` | Forget the link and remove the header and marks. The Doc is unchanged |

Every command except a `--dry-run` requires a
[trusted runtime](installation.md#runtime-write-protection). Commits go through
the same path as `baton artifacts commit`: they sync with and push to the artifact
checkout's `origin` when it has one (`pull --no-push` commits locally only). A pull
syncs once before its first link and pushes once after its last.

`baton status --json` lists the links as `gdoc_links`, each with `path` and `url`.
A missing or malformed state file shows no links.

## State

- `gdoc-links.json` in the artifact directory holds the links:
  `{"links": [{"path": "infra.md", "document_id": "..."}]}`. Link and unlink edit
  and commit it.
- Each linked file starts with a one-line HTML comment naming the Doc.
- Every Mermaid block in a linked file carries a mark on the line directly before
  its opening fence:

  | Mark | Meaning |
  |---|---|
  | `<!-- baton-gdoc image=ID sha256=HASH -->` | Rendered as the Doc's image `ID` from source with this SHA-256. A different or missing hash means the image is stale |
  | `<!-- baton-gdoc placeholder=NAME -->` | Pending. The Doc holds a paragraph whose whole text is `baton-gdoc-diagram-NAME` (any case) where the image goes. `NAME` uses lowercase letters, digits and hyphens |

A comment elsewhere, or inside another code fence, is not a mark. Pull and push
refuse a file with an unmarked Mermaid block or a repeated image ID or name.
Mermaid blocks must be at the top level: a Mermaid fence inside a blockquote,
list item or indented block is refused by every command.

## Link

Both forms refuse a path or Doc that is already linked, and a Doc with several
tabs.

Adopting pairs the unmarked Mermaid blocks, in order, with the Doc's inline
images that no mark names, and marks each with the block's current hash, which
assumes the Doc's images are up to date. The counts must match; otherwise link
refuses and lists those images with the text before each, so blocks can be marked
by hand first. Once every block is marked, other images in the Doc are simply not
archived.

Creating imports the Markdown with each Mermaid block replaced by a placeholder
paragraph, marks the blocks `placeholder=1`, `placeholder=2` and so on, records
the link, then runs push. A create that fails after the Doc exists leaves either
a linked file with pending diagrams (run `push` again) or an unlinked Doc whose
URL was printed (adopt it).

## Pull

Pull reads the Doc's revision, exports it as Markdown, then reads its structure,
which carries the revision again; if it moved, it retries once, then refuses. In
the export:

| Found | Result |
|---|---|
| Image a mark names | That mark and block |
| Placeholder paragraph a pending mark names | That mark and block |
| Placeholder paragraph no pending mark names | Kept as text |
| Image no mark names | A one-line "not archived" note with its ID |
| Marked image no longer in the Doc, and no unmarked image | Block dropped and reported |
| Marked image no longer in the Doc, and an unmarked image | Refused: fix the mark's ID, or delete the block and commit |
| Pending placeholder missing or repeated | Refused: fix the Doc, or delete the block and commit |
| Marked image not alone in its own paragraph | Refused: move it to its own paragraph in the Doc |
| Export and structure image counts differ | Refused: set the text-wrapped image to "In line" |

Refusals involving images list the Doc's unmarked images in order with the text
before each. A fenced block labelled `mermaid` in the export loses the label, and
a restored block gets a blank line on each side where the export joined it to the
neighbouring text with hard line breaks.
Everything else is kept as exported, and pull reports the number of pending
suggestions: suggested insertions export as ordinary text and text suggested for
deletion is still exported.

The write happens under the artifact commit lock after the checkout synced. Pull
refuses unless the file is tracked, has no uncommitted changes and still equals
what it read before the export, except when it already holds the new content, so
rerunning a pull whose commit failed completes it. An unchanged Doc makes no
commit. A refused link is skipped, the others commit, and the command exits 1.

Pull publishes its commits as other artifact commits do. A project with its own
[artifacts branch](configuration.md#dedicated-artifacts-branch) pushes that
branch, with no default-branch sync. With `dedicated_artifacts_branch` on, the
shared checkout publishes through a pull request that Baton merges instead of a
push to the default branch.

The first pull after adopting a Doc rewrites formatting and brings in every edit
made in the Doc so far; `--dry-run` shows that diff first.

## Push

Before anything changes, push checks that the Markdown is tracked and committed,
that the artifact checkout fetched and is not behind `origin` (it fast-forwards
when only artifacts changed), that every `image=ID` is in the Doc, and that each
pending placeholder is in the Doc exactly once as its own paragraph outside
tables and lists.

It then renders each pending or stale block with `mmdc`, uploads each PNG as a
temporary private Doc to get an image URI, and, under the artifact commit lock,
sends one `documents.batchUpdate` guarded by the Doc's revision: a stale image is
deleted and the new one inserted in its place (re-rendering keeps the new
diagram's shape but detaches a comment placed only on the old image, and shrinks
the anchor of one spanning text and the image to its text part), and a
placeholder paragraph's text is replaced by the image. The marks are rewritten
with the new image IDs and hashes and committed. Temporary Docs are deleted
afterwards.

On a project with its own artifacts branch, push checks only that the worktree is
on that branch: the branch has a single writer and is normally behind the default
branch. With `dedicated_artifacts_branch` on, a shared checkout that has
unpublished commits publishes them first through a pull request, which leaves it
behind rather than diverged, so it can fast-forward before the "not behind" check.

When Google refuses the `batchUpdate` because the Doc's revision moved after push
read it (any edit or comment moves it), nothing was written; push says so, and
pushing again is enough. Any other `batchUpdate` that fails or times out may still
have changed the Doc. Push then lists the Doc's unmarked images: for a diagram
whose new image is listed, set its mark to `image=ID` without a hash and commit;
the next push re-renders it. If the Doc changed but the commit failed, the file
already holds the new marks: fix the cause shown and run the printed commit
command. Printed recovery commands carry `--selector` with the project's UUID, so
they work from any directory.

To add a diagram, put a paragraph `baton-gdoc-diagram-NAME` in the Doc where it
belongs, add the Mermaid block to the Markdown marked `placeholder=NAME`, commit,
and push.

## Limits

Single-tab Docs only. Comments and suggestions are left to `gws`; Baton never
edits the text of an existing Doc. Images that are not Mermaid diagrams become
notes. Pulls happen when an agent or person runs them.
