from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from baton.skills_install import MANIFEST, RETIRED_SKILLS, SKILLS, WORKFLOW, bundled_skills, install_skills


class SkillsInstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / "config.json"
        environment = {key: value for key, value in os.environ.items() if not key.startswith("BATON_")}
        environment.update(HOME=str(self.root), BATON_CONFIG_FILE=str(self.config))
        self.enterContext(patch.dict(os.environ, environment, clear=True))

    def test_install_and_update_managed_links_preserves_user_skills(self) -> None:
        target = self.root / "skills"
        target.mkdir()
        custom = target / "custom-plan"
        custom.mkdir()
        (custom / "SKILL.md").write_text("custom")
        installed = install_skills([target])
        self.assertEqual(len(installed), len(SKILLS))
        self.assertTrue(all((path / "SKILL.md").is_file() for path in installed))
        self.assertTrue(all(path.is_symlink() for path in installed if path.name != WORKFLOW))
        self.assertFalse((target / WORKFLOW).is_symlink())
        self.assertEqual(install_skills([target]), installed)
        reference = Path('baton-plan/references/validation.md')
        self.assertEqual((target / reference).read_bytes(), (bundled_skills() / reference).read_bytes())
        self.assertTrue((target / reference.parent / '../../baton-workflow/references/coordination.md').resolve().is_file())
        self.assertEqual((custom / "SKILL.md").read_text(), "custom")
        manifest = json.loads((target / MANIFEST).read_text())
        self.assertEqual(set(manifest["links"]), set(SKILLS) - {WORKFLOW})
        executor = (target / "baton-review-executor/SKILL.md").read_text()
        self.assertIn("baton review run --kind plan", executor)
        self.assertTrue((target / "baton-plan/SKILL.md").resolve().parent.joinpath(
            "../baton-review-executor/SKILL.md").resolve().is_file())


    def test_update_repoints_owned_links_to_new_bundle(self) -> None:
        target = self.root / "skills"
        first, second = self.root / "release-1", self.root / "release-2"
        shutil.copytree(bundled_skills(), first)
        shutil.copytree(bundled_skills(), second)
        install_skills([target], first)
        shutil.rmtree(first)
        install_skills([target], second)
        self.assertEqual((target / "baton-retro").resolve(), (second / "baton-retro").resolve())
        manifest = json.loads((target / MANIFEST).read_text())
        self.assertEqual(manifest["links"]["baton-retro"], str((second / "baton-retro").resolve()))

    def test_reinstall_removes_retired_skills_that_the_manifest_owns(self) -> None:
        target, other = self.root / "skills", self.root / "other"
        previous = self.root / "release-1"
        shutil.copytree(bundled_skills(), previous)
        for name in RETIRED_SKILLS:
            (previous / name).mkdir()
            (previous / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
        # A manifest as the previous release wrote it: the retired executors were managed links.
        install_skills([target, other], previous)
        for directory in (target, other):
            manifest = json.loads((directory / MANIFEST).read_text())
            manifest["links"].update({name: str(previous / name) for name in RETIRED_SKILLS})
            (directory / MANIFEST).write_text(json.dumps(manifest))
            for name in RETIRED_SKILLS:
                (directory / name).symlink_to(previous / name, target_is_directory=True)
        # A link the user repointed is no longer Baton's to remove.
        repointed = other / RETIRED_SKILLS[0]
        repointed.unlink()
        repointed.symlink_to(self.root, target_is_directory=True)
        install_skills([target, other])
        for name in RETIRED_SKILLS:
            self.assertFalse((target / name).is_symlink() or (target / name).exists())
        self.assertEqual(os.readlink(repointed), str(self.root))
        self.assertFalse((other / RETIRED_SKILLS[1]).is_symlink())
        for directory in (target, other):
            manifest = json.loads((directory / MANIFEST).read_text())
            self.assertEqual(set(manifest["links"]), set(SKILLS) - {WORKFLOW})
            self.assertTrue((directory / "baton-review-executor/SKILL.md").is_file())
        # The next install no longer knows the repointed link and leaves it alone.
        install_skills([target, other])
        self.assertEqual(os.readlink(repointed), str(self.root))

    def test_manifest_with_unknown_skill_is_rejected(self) -> None:
        target = self.root / "skills"
        install_skills([target])
        manifest = json.loads((target / MANIFEST).read_text())
        manifest["links"]["baton-unknown"] = str(self.root)
        (target / MANIFEST).write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "Invalid managed skill links"):
            install_skills([target])

    def test_conflict_preflight_does_not_install_other_targets(self) -> None:
        a, b = self.root / "a", self.root / "b"
        b.mkdir()
        (b / "baton-retro").mkdir()
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            install_skills([a, b])
        self.assertFalse((a / "baton-plan").exists())
        self.assertTrue((b / "baton-retro").is_dir())

    def test_modified_or_unmanaged_symlink_is_preserved(self) -> None:
        target = self.root / "skills"
        install_skills([target])
        path = target / "baton-retro"
        path.unlink()
        path.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            install_skills([target])
        self.assertEqual(os.readlink(path), str(self.root / "missing"))


    def test_cli_uses_isolated_home_defaults(self) -> None:
        env = os.environ | {"HOME": str(self.root), "BATON_ALLOW_UNTRUSTED_WRITES": "1"}
        result = subprocess.run([sys.executable, "-m", "baton.skills_install", "install"], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in (".agents", ".claude"):
            self.assertTrue((self.root / name / "skills/baton-plan/SKILL.md").is_file())

    def test_cli_defaults_add_detected_providers_and_report_skips_and_stale_copies(self) -> None:
        binaries = self.root / "bin"
        binaries.mkdir()
        (binaries / "agent").write_text("#!/bin/sh\n")
        (binaries / "agent").chmod(0o755)
        stale = self.root / ".gemini/config/skills/baton-retro"
        stale.mkdir(parents=True)
        env = os.environ | {"HOME": str(self.root), "BATON_ALLOW_UNTRUSTED_WRITES": "1", "PATH": str(binaries)}
        result = subprocess.run([sys.executable, "-m", "baton.skills_install", "install"], env=env, text=True,
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in (".agents", ".claude"):
            self.assertTrue((self.root / name / "skills/baton-plan/SKILL.md").is_file())
        self.assertFalse((self.root / ".gemini/antigravity-cli/skills").exists())
        self.assertIn(f"Skipped {self.root / '.gemini/antigravity-cli/skills'}: agy not found", result.stdout)
        self.assertIn(f"Warning: {self.root / '.gemini/config/skills'} contains baton-* skills Baton does not "
                      "manage (baton-retro).", result.stderr.splitlines()[0])
        self.assertTrue(stale.is_dir())
        (binaries / "agy").write_text("#!/bin/sh\n")
        (binaries / "agy").chmod(0o755)
        result = subprocess.run([sys.executable, "-m", "baton.skills_install", "install"], env=env, text=True,
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / ".gemini/antigravity-cli/skills/baton-plan/SKILL.md").is_file())
        self.assertNotIn("Skipped", result.stdout)
        explicit = self.root / "explicit"
        result = subprocess.run([sys.executable, "-m", "baton.skills_install", "install", "--target", str(explicit)],
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((explicit / "baton-plan").is_symlink())

    def description(self, target: Path) -> str:
        content = (target / WORKFLOW / "SKILL.md").read_text()
        line = next(line for line in content.splitlines() if line.startswith("description:"))
        return json.loads(line.removeprefix("description: "))

    def test_configured_description_and_resource_links_preserve_bundle(self) -> None:
        slots = self.root / 'slots: "quoted" \\ path\nnext line'
        self.config.write_text(json.dumps({"paths": {"slots_root": str(slots)}}))
        source = bundled_skills()
        before = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
        targets = [self.root / "codex", self.root / "claude"]
        install_skills(targets)
        for target in targets:
            with self.subTest(target=target):
                self.assertIn(str(slots.resolve()), self.description(target))
                self.assertNotIn("{{BATON_SLOTS_ROOT}}", self.description(target))
                workflow = (target / WORKFLOW).resolve()
                self.assertEqual((workflow / "references/startup.md").read_bytes(),
                                 (source / WORKFLOW / "references/startup.md").read_bytes())
                self.assertEqual((workflow / "agents/openai.yaml").read_bytes(),
                                 (source / WORKFLOW / "agents/openai.yaml").read_bytes())
                self.assertEqual((workflow / "../baton-plan/SKILL.md").read_bytes(),
                                 (source / "baton-plan/SKILL.md").read_bytes())
        after = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_installed_skill_links_resolve_without_repository_docs(self) -> None:
        bundle = self.root / "package" / "skills"
        shutil.copytree(bundled_skills(), bundle)
        target = self.root / "custom-target"
        install_skills([target], bundle)
        checked = set()
        for name in SKILLS:
            for packaged in (bundle / name).rglob("*.md"):
                relative = packaged.relative_to(bundle)
                source = target / relative
                checked.add(relative)
                for link in re.findall(r"\]\(([^)]+)\)", source.read_text()):
                    if "://" in link:
                        continue
                    path, _, fragment = link.partition("#")
                    destination = source.parent / path if path else source
                    with self.subTest(source=str(source.relative_to(target)), link=link):
                        self.assertTrue(destination.is_file(), str(destination))
                        self.assertTrue(destination.resolve().is_relative_to(bundle) or
                                        destination.resolve().is_relative_to(target))
                        if fragment:
                            headings = re.findall(r"^#{1,6} (.+)$", destination.read_text(), re.MULTILINE)
                            anchors = {re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-")
                                       for heading in headings}
                            self.assertIn(fragment, anchors)
        for reference in ("startup.md", "coordination.md", "branches-lifecycle.md"):
            self.assertIn(Path(WORKFLOW) / "references" / reference, checked)

    def test_installed_workflow_supports_discovery_before_ownership(self) -> None:
        target = self.root / "skills"
        install_skills([target])
        self.assertIn("from anywhere", self.description(target))
        workflow = target / WORKFLOW
        metadata = (workflow / "agents/openai.yaml").read_text()
        self.assertIn("from anywhere", metadata)
        self.assertIn("allow_implicit_invocation: true", metadata)
        startup = (workflow / "references/startup.md").read_text()
        self.assertLess(startup.index("## Discover or revisit"), startup.index("## Resolve the project"))
        for command in ("baton search", "baton session fork --selector FULL_UUID"):
            self.assertIn(command, startup)
        self.assertIn("Discovery itself does not authorize", startup)

    def test_install_uses_source_description_template(self) -> None:
        source = self.root / "bundle"
        shutil.copytree(bundled_skills(), source)
        skill = source / WORKFLOW / "SKILL.md"
        template = 'Use slots at {{BATON_SLOTS_ROOT}}; preserve "custom wording" and {other braces}.'
        lines = skill.read_text().splitlines(keepends=True)
        index = next(index for index, line in enumerate(lines) if line.startswith("description:"))
        lines[index] = "description: " + json.dumps(template) + "\n"
        skill.write_text("".join(lines))
        target = self.root / "skills"
        install_skills([target], source)
        expected = template.replace("{{BATON_SLOTS_ROOT}}", str(self.root / "dev/baton-wt"))
        self.assertEqual(self.description(target), expected)
        installed = (target / WORKFLOW / "SKILL.md").read_text()
        self.assertEqual(installed.partition("\n---\n")[2], skill.read_text().partition("\n---\n")[2])

    def test_invalid_description_placeholder_refuses_before_installation(self) -> None:
        source = self.root / "bundle"
        shutil.copytree(bundled_skills(), source)
        skill = source / WORKFLOW / "SKILL.md"
        lines = skill.read_text().splitlines(keepends=True)
        index = next(index for index, line in enumerate(lines) if line.startswith("description:"))
        target = self.root / "skills"
        for template in ("No slot placeholder", "{{BATON_SLOTS_ROOT}} and {{BATON_SLOTS_ROOT}}", 42):
            with self.subTest(template=template):
                lines[index] = "description: " + json.dumps(template) + "\n"
                skill.write_text("".join(lines))
                with self.assertRaisesRegex(ValueError, "Expected one .* placeholder"):
                    install_skills([target], source)
                self.assertFalse(target.exists())

    def test_default_paths_environment_precedence_and_reinstall(self) -> None:
        target = self.root / "skills"
        install_skills([target])
        self.assertIn(str(self.root / "dev/baton-wt"), self.description(target))
        self.config.write_text(json.dumps({"paths": {"slots_root": str(self.root / "saved-slots")}}))
        install_skills([target])
        self.assertIn(str(self.root / "saved-slots"), self.description(target))
        os.environ["BATON_SLOTS_ROOT"] = str(self.root / "override-slots")
        install_skills([target])
        self.assertIn(str(self.root / "override-slots"), self.description(target))
        os.environ["BATON_SLOTS_ROOT"] = str(self.root / "later-slots")
        self.assertIn(str(self.root / "override-slots"), self.description(target))
        install_skills([target])
        self.assertIn(str(self.root / "later-slots"), self.description(target))

    def test_migrate_owned_legacy_workflow_symlink(self) -> None:
        target = self.root / "skills"
        target.mkdir()
        old = self.root / "old-bundle"
        shutil.copytree(bundled_skills(), old)
        links = {name: str(old / name) for name in SKILLS}
        for name, destination in links.items():
            (target / name).symlink_to(destination)
        (target / MANIFEST).write_text(json.dumps({"format": 1, "links": links}))
        shutil.rmtree(old)
        install_skills([target])
        self.assertFalse((target / WORKFLOW).is_symlink())
        self.assertTrue((target / WORKFLOW / "references/startup.md").is_file())
        self.assertEqual(json.loads((target / MANIFEST).read_text())["format"], 2)
        self.assertIn(str(self.root / "dev/baton-wt"), self.description(target))

    def test_generated_workflow_edits_are_preserved_before_any_target_updates(self) -> None:
        for change in ("content", "extra", "resource", "file-link", "directory-link", "missing"):
            with self.subTest(change=change):
                a, b = self.root / change / "a", self.root / change / "b"
                install_skills([a, b])
                workflow = b / WORKFLOW
                if change == "content":
                    (workflow / "SKILL.md").write_text("user edits")
                elif change == "extra":
                    (workflow / "notes.txt").write_text("user notes")
                elif change == "resource":
                    (workflow / "references").unlink()
                    (workflow / "references").symlink_to(self.root / "missing")
                elif change == "file-link":
                    original = (workflow / "SKILL.md").read_text()
                    external = self.root / "external.md"
                    external.write_text(original)
                    (workflow / "SKILL.md").unlink()
                    (workflow / "SKILL.md").symlink_to(external)
                elif change == "directory-link":
                    moved = b / "moved"
                    workflow.rename(moved)
                    workflow.symlink_to(moved)
                else:
                    (workflow / "SKILL.md").unlink()
                manifest = (a / MANIFEST).read_bytes()
                previous = (a / WORKFLOW / "SKILL.md").read_bytes()
                os.environ["BATON_SLOTS_ROOT"] = str(self.root / "new-slots" / change)
                with self.assertRaisesRegex(ValueError, "modified managed"):
                    install_skills([a, b])
                self.assertEqual((a / MANIFEST).read_bytes(), manifest)
                self.assertEqual((a / WORKFLOW / "SKILL.md").read_bytes(), previous)
                if change == "content":
                    self.assertEqual((workflow / "SKILL.md").read_text(), "user edits")
                elif change == "extra":
                    self.assertEqual((workflow / "notes.txt").read_text(), "user notes")

    def test_workflow_resources_update_after_old_distribution_is_removed(self) -> None:
        target = self.root / "skills"
        old, new = self.root / "old", self.root / "new"
        shutil.copytree(bundled_skills(), old)
        shutil.copytree(bundled_skills(), new)
        install_skills([target], old)
        shutil.rmtree(old)
        install_skills([target], new)
        self.assertEqual((target / WORKFLOW / "references").resolve(), new / WORKFLOW / "references")
        self.assertEqual((target / WORKFLOW / "agents").resolve(), new / WORKFLOW / "agents")

    def test_invalid_generated_ownership_is_rejected(self) -> None:
        target = self.root / "skills"
        install_skills([target])
        original = json.loads((target / MANIFEST).read_text())
        for bad in (None, {}, {"sha256": "x" * 64, "links": {}},
                    {"sha256": "a" * 64, "links": {"../outside": "/external"}}):
            with self.subTest(ownership=bad):
                manifest = original | {"workflow": bad}
                (target / MANIFEST).write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, "Invalid managed workflow"):
                    install_skills([target])

    def test_bundled_policies_and_names(self) -> None:
        for name in SKILLS:
            skill = bundled_skills() / name
            with self.subTest(name=name):
                self.assertIn(f"name: {name}\n", (skill / "SKILL.md").read_text())
                if name == WORKFLOW:
                    self.assertNotIn("disable-model-invocation: true", (skill / "SKILL.md").read_text())
                    self.assertIn("allow_implicit_invocation: true", (skill / "agents/openai.yaml").read_text())
                else:
                    self.assertIn("disable-model-invocation: true", (skill / "SKILL.md").read_text())
                    self.assertIn("allow_implicit_invocation: false", (skill / "agents/openai.yaml").read_text())


if __name__ == "__main__":
    unittest.main()
