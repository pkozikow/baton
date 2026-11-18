---
name: baton-gdoc
description: >-
  Share a Markdown project artifact as a Google Doc and keep the Markdown as its
  Git archive: link, pull, push Mermaid diagrams, and recover from refusals.
argument-hint: "[link|pull|push|unlink] [artifact-path]"
disable-model-invocation: true
---

# Google Doc archives

Use this skill before linking a project artifact to a Google Doc, and before
relying on or changing an artifact that `baton status --json` lists under
`gdoc_links`. Take each link's path and Doc URL from there.

`baton gdoc` needs the `gws` CLI, signed in with Docs and Drive access, and
`mmdc` (Mermaid CLI) to render diagrams. A missing tool fails with a message
naming the fix; nothing else in Baton needs either.

## Rules

1. **Text flows Doc → Markdown.** After linking, never edit the linked Markdown's
   text. `pull` overwrites it from the Doc; Git keeps every earlier version.
2. **Diagram source flows Markdown → Doc.** The Doc holds only a rendered image of
   each Mermaid block. Change a diagram by editing its block in the Markdown,
   committing, and running `push`, which replaces only that image.
3. **Baton is the archive, not the editor.** Edit Doc text and read or answer
   comments with `gws` directly (Docs `documents batchUpdate`; Drive `comments`
   and `replies`), then pull. Baton never edits the text of an existing Doc.

Each linked file starts with a one-line header comment naming the Doc. Every
Mermaid block carries a mark on the line before its fence:

- `<!-- baton-gdoc image=ID sha256=HASH -->`: rendered as the Doc's image `ID`
  from the source with that hash. A missing or different hash means push
  re-renders it.
- `<!-- baton-gdoc placeholder=NAME -->`: pending. The Doc holds a paragraph whose
  whole text is `baton-gdoc-diagram-NAME` where the image goes.

Marks are the only part of a linked file to edit by hand, besides Mermaid source.

## Link

Link only when the user wants the artifact reviewed or edited in Google Docs.
From then on the Doc is the source of truth for its text.

- **Adopt an existing Doc:** `baton gdoc link PATH DOC_URL`. Unmarked Mermaid
  blocks are paired in order with the Doc's images that no mark names; nothing is
  written to the Doc. When the counts differ, link refuses and lists the Doc's
  unnamed images with the text before each: mark the blocks by hand
  (`image=ID`), commit, link again. Images left over once every block is marked
  (screenshots) are archived as notes. For a Doc that still shows Mermaid as code,
  mark each block `placeholder=NAME`, put the placeholder paragraphs in the Doc,
  link, then push.
- **Create a Doc:** `baton gdoc link PATH [--title T]`. Imports the Markdown into
  a new Doc, prints its URL, records the link and pushes the diagrams. If the push
  fails, the link stands with pending marks: fix the cause, run `baton gdoc push
  PATH`. If recording fails, adopt the printed URL instead.
- **Unlink:** `baton gdoc unlink PATH` removes the header, marks and link and
  leaves the Doc alone. To start over on a Doc nobody has commented on, unlink and
  create again.

Multi-tab Docs and files outside the project's artifact directory are refused.

## Pull

`baton gdoc pull [PATH...] [--dry-run] [--no-push]` exports each linked Doc,
restores the Mermaid blocks where their images are, and commits one commit per
changed file. An unchanged Doc makes no commit. A refused link is skipped while
the others commit; the command then exits non-zero.

Pull when it matters, as when deciding to pull a repository's main branch:

- right after linking (the first pull rewrites formatting and brings in every
  edit made in the Doc so far; `--dry-run` shows the diff first);
- before relying on an archive;
- after changing the Doc yourself;
- at milestones such as plan approval or handoff.

Pull reports pending suggestions: suggested insertions are archived as ordinary
text and text suggested for deletion is still archived. Resolve suggestions in
the Doc when the archive must be exact.

## Change or add a diagram

Before pushing a changed diagram, list the Doc's open comments with `gws` and
handle any about that diagram first. Re-rendering deletes and reinserts the image,
so a comment placed only on the diagram loses its anchor (the thread stays open).
Comments on text keep theirs; a comment spanning text and the diagram keeps its
anchor, but the anchor shrinks to the text part.

- **Change:** edit the Mermaid block, commit the Markdown, `baton gdoc push PATH`.
- **Add:** put a paragraph `baton-gdoc-diagram-NAME` in the Doc where the diagram
  belongs (lowercase letters, digits and hyphens; the editor's capitalised first
  letter is fine). Add the Mermaid block to the Markdown marked
  `placeholder=NAME`, commit, `baton gdoc push PATH`. A pull between the steps
  places the pending block where the placeholder is.
- **Delete:** delete the image in the Doc and pull; the block is dropped. When
  the Doc has unmarked images, pull refuses instead: delete the block from the
  Markdown too, commit, pull.

`push --dry-run` lists the diagrams it would render. Push refuses unless the
Markdown is committed, the artifact checkout is up to date with origin, every
`image=ID` is in the Doc, and each placeholder is in the Doc exactly once as its
own paragraph outside tables and lists.

## Refusals

Refusals that involve images list the Doc's unmarked images in order, each with
the text just before it. Take IDs from that list.

| Message says | Do |
|---|---|
| Unmarked Mermaid block | Add a mark before the fence: `image=ID` or `placeholder=NAME`, commit |
| Several blocks marked the same | Make each mark unique, commit |
| Mermaid fence inside a blockquote, list item or indented block | Move the block to the top level, commit |
| Image no longer in the Doc and the Doc has unmarked images | It may have been cut and pasted: put the listed ID in the mark, or delete the block; commit, pull |
| Placeholder missing from the Doc or appears several times | Add or fix the placeholder paragraph in the Doc, or delete the block and commit. If a push was interrupted, change the mark to `image=ID` with the listed ID, commit |
| Image not alone in its own paragraph | Move the image to its own paragraph in the Doc, or delete the block and commit |
| Export and Doc image counts differ | Set the text-wrapped image's wrapping to "In line" in the Doc, pull again |
| Uncommitted or staged changes | Commit, unstage or discard them; remember rule 1 |
| Markdown or Doc changed during the pull | Pull again |
| Image not in the Doc; pull first | Pull, then push |
| Fetch failed, or checkout behind origin | Retry when the remote is reachable; a checkout that cannot fast-forward needs the user |
| Push: the Doc changed while the push ran | Someone edited or commented on the Doc meanwhile; nothing was written. Push again |
| Push: update may still have been applied | For each listed new image, set the block's mark to `image=ID` without `sha256`, commit; the next push re-renders it. If none is listed, push again |
| Push: the Doc was updated, commit failed | The file holds the new marks; fix the cause shown, then run the printed `baton artifacts commit` command |
| Several tabs | Not supported; use a single-tab Doc |
| Already linked | Unlink first, or use the existing link |

## Multi-branch projects

Shared artifacts belong to the parent. Workers do not pull, push, link or unlink
them; ask the parent through the blackboard, naming the file and what changed.
From a branch checkout, the commands refuse other branches' subplans, and a pull
of all links skips them.
