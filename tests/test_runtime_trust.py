import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class RuntimeTrustTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        repository = Path(__file__).resolve().parents[1]
        shutil.copytree(repository / "baton", self.source / "baton", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(repository / "agent-skills", self.source / "agent-skills", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(repository / "blackboard.py", self.source / "blackboard.py")
        (self.source / "pyproject.toml").write_text('[project]\nname = "baton"\nversion = "0.1.0"\n')
        self.home = self.root / "home"
        self.config = self.home / ".config/baton/config.json"
        self.config.parent.mkdir(parents=True)
        self.config.write_text('{"branch_prefix": "original"}\n')
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("BATON_") and key not in ("PYTHONPATH", "XDG_CONFIG_HOME", "XDG_STATE_HOME")}
        self.env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / ".config"),
                        XDG_STATE_HOME=str(self.home / ".local/state"),
                        GIT_CONFIG_COUNT="4", GIT_CONFIG_KEY_0="user.name", GIT_CONFIG_VALUE_0="Runtime test",
                        GIT_CONFIG_KEY_1="user.email", GIT_CONFIG_VALUE_1="runtime@example.test",
                        GIT_CONFIG_KEY_2="commit.gpgsign", GIT_CONFIG_VALUE_2="false",
                        GIT_CONFIG_KEY_3="core.hooksPath", GIT_CONFIG_VALUE_3="/dev/null")
        self.git("init", "-b", "main")
        self.git("add", ".")
        self.git("-c", "user.name=Runtime test", "-c", "user.email=runtime@example.test",
                 "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-m", "Initial runtime")
        self.revision = self.git("rev-parse", "HEAD")
        self.runtime = self.source

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.source), *args], env=self.env,
                              check=True, capture_output=True, text=True).stdout.strip()

    def cli(self, *args: str, module: str = "baton.cli", cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        code = ("import importlib,sys; sys.path.insert(0,sys.argv.pop(1)); module=sys.argv.pop(1); "
                "sys.exit(importlib.import_module(module).main())")
        return subprocess.run([sys.executable, "-I", "-B", "-c", code, str(self.runtime), module, *args],
                              env=self.env, cwd=cwd or self.home, capture_output=True, text=True)

    def snapshot(self) -> dict[str, bytes | None]:
        return {str(path.relative_to(self.home)): path.read_bytes() if path.is_file() else None
                for path in self.home.rglob("*")}

    def assert_refused(self, *args: str, module: str = "baton.cli") -> None:
        before = self.snapshot()
        result = self.cli(*args, module=module)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("requires a clean revision of main", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def installed(self, provenance: object) -> None:
        self.runtime = self.root / "site-packages"
        package = self.runtime / "baton"
        shutil.copytree(self.source / "baton", package)
        shutil.copytree(self.source / "agent-skills", package / "skills")
        shutil.copyfile(self.source / "blackboard.py", self.runtime / "blackboard.py")
        self.stamp = package / "_build_info.json"
        self.stamp.write_text(json.dumps(provenance))
        distribution = self.runtime / "baton-0.1.0.dist-info"
        distribution.mkdir()
        (distribution / "METADATA").write_text("Name: baton\nVersion: 0.1.0\n")
        (distribution / "direct_url.json").write_text(json.dumps({"url": self.source.as_uri(), "dir_info": {}}))

    def test_clean_main_source_can_write_from_another_directory(self) -> None:
        result = self.cli("config", "set", "--branch-prefix", "changed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.config.read_text())["branch_prefix"], "changed")

    def test_feature_and_detached_source_refuse_writes(self) -> None:
        for arguments in (("switch", "-c", "feature"), ("switch", "--detach", "main")):
            with self.subTest(arguments=arguments):
                self.git(*arguments)
                self.assert_refused("config", "init")

    def test_dirty_staged_and_untracked_main_refuse_writes(self) -> None:
        tracked = self.source / "pyproject.toml"
        tracked.write_text(tracked.read_text() + "# local change\n")
        self.assert_refused("config", "init")
        self.git("add", "pyproject.toml")
        self.assert_refused("config", "init")
        self.git("reset", "--hard", "HEAD")
        (self.source / "scratch.txt").write_text("untracked\n")
        self.assert_refused("config", "init")

    def test_copied_source_cannot_inherit_trust_from_an_enclosing_repository(self) -> None:
        (self.source / ".git/info/exclude").write_text("/ignored/\n")
        self.runtime = self.source / "ignored/copied-runtime"
        shutil.copytree(self.source / "baton", self.runtime / "baton")
        shutil.copyfile(self.source / "blackboard.py", self.runtime / "blackboard.py")
        shutil.copyfile(self.source / "pyproject.toml", self.runtime / "pyproject.toml")
        self.assertEqual(self.git("status", "--porcelain"), "")
        before = self.snapshot()
        result = self.cli("config", "init")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("not an exact Git checkout root", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_removing_source_metadata_does_not_make_a_dirty_checkout_trusted(self) -> None:
        (self.source / "pyproject.toml").unlink()
        (self.source / "baton/_build_info.json").write_text(json.dumps(
            {"revision": self.revision, "dirty": False, "branch": "refs/heads/main"}))
        self.assert_refused("config", "init")

    def test_all_public_write_entrypoints_refuse_without_touching_home(self) -> None:
        self.git("switch", "-c", "feature")
        commands = (
            ("baton.cli", ("config", "set", "--branch-prefix", "changed")),
            ("baton.cli", ("skills", "install")),
            ("baton.configuration", ("init",)),
            ("baton.configuration", ("set", "--branch-prefix", "changed")),
            ("baton.skills_install", ("install",)),
            ("baton.skills_install", ("install", "--target", str(self.home / ".agents/skills"))),
            ("baton.retro_cron", ("install",)),
            ("baton.retro_cron", ("run",)),
            ("baton.retro_cron", ("install-weekly", "--provider", "codex")),
            ("baton.retro_cron", ("run-weekly", "--provider", "codex")),
            ("baton.cli", ("retro", "summarize", "--provider", "codex")),
            ("baton.cli", ("retro", "pending")),
            ("baton.cli", ("init",)),
            ("baton.cli", ("create", "example", "--repos", "alpha")),
            ("baton.cli", ("cleanup",)),
            ("baton.cli", ("branch", "add", "alpha", "member")),
            ("baton.cli", ("stack", "status", "alpha")),
            ("baton.cli", ("blackboard", "post", "--agent", "test", "--expected-generation", "unused")),
            ("baton.cli", ("blackboard", "export", "--expected-generation", "unused")),
            ("baton.cli", ("blackboard", "ask", "--agent", "test", "--to", "parent", "--text", "x", "--expected-generation", "unused")),
            ("baton.cli", ("task", "create", "--title", "test", "--agent", "test", "--expected-generation", "unused")),
            ("baton.cli", ("task", "settle", "--id", "x", "--expected-version", "1", "--outcome", "failed", "--summary", "x",
                           "--agent", "test", "--expected-generation", "unused")),
            ("baton.cli", ("artifacts", "commit", "-m", "test", str(self.home / "plan.md"))),
            ("baton.cli", ("session", "fork")),
            ("baton.cli", ("open-plan",)),
        )
        for module, args in commands:
            with self.subTest(module=module, args=args):
                self.assert_refused(*args, module=module)

    def test_reads_help_and_dry_runs_do_not_require_trust(self) -> None:
        self.git("switch", "-c", "feature")
        before = self.snapshot()
        for module, args in (("baton.cli", ("--help",)), ("baton.cli", ("config", "show")),
                             ("baton.configuration", ("show",)), ("baton.skills_install", ("--help",)),
                             ("baton.retro_cron", ("run", "--help")),
                             ("baton.cli", ("status", "--json")), ("baton.cli", ("cleanup", "--dry-run")),
                             ("baton.cli", ("retro", "pending", "--dry-run")),
                             ("baton.cli", ("retro", "summarize", "--dry-run", "--json")),
                             ("baton.retro_cron", ("run-weekly", "--dry-run", "--json"))):
            with self.subTest(module=module, args=args):
                result = self.cli(*args, module=module)
                self.assertNotIn("untrusted Baton runtime", result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_untrusted_runtime_can_inspect_an_active_project_without_writing_state(self) -> None:
        result = self.cli("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        coordinator = self.home / "dev/baton-wt/p1"
        result = self.cli("create", "example", "--repos", str(self.source), cwd=coordinator)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.git("switch", "-c", "feature")
        board = ("--selector", "example", "--expected-generation",
                 json.loads(self.cli("status", "example", "--json").stdout)["blackboard"]["generation"])
        cursor = json.loads(self.cli("blackboard", "read", "--selector", "example", "--json").stdout)["cursor"]
        before = self.snapshot()
        for args in (("list",), ("status", "example", "--json"), ("path", "example"),
                     ("handoff", "--selector", "example", "--agent", "test", "--repo", "source"),
                     ("blackboard", "read", "--selector", "example", "--json"),
                     ("blackboard", "wait", *board, "--since", cursor, "--timeout", "0"),
                     ("blackboard", "agents", *board),
                     ("task", "list", "--selector", "example"), ("cleanup", "example", "--dry-run")):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_untrusted_read_cannot_recover_a_registry_by_writing_it(self) -> None:
        result = self.cli("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        layout = self.home / "dev/baton-projects/.git/baton/v2"
        layout.rename(layout.with_name("v2.tmp"))
        self.git("switch", "-c", "feature")
        self.assert_refused("list")

    def test_explicit_development_override_allows_isolated_tests(self) -> None:
        self.git("switch", "-c", "feature")
        self.env["BATON_ALLOW_UNTRUSTED_WRITES"] = "1"
        result = self.cli("config", "init")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_redirecting_only_config_does_not_bypass_the_guard(self) -> None:
        self.git("switch", "-c", "feature")
        self.env["BATON_CONFIG_FILE"] = str(self.config)
        self.assert_refused("config", "init")

    def test_installed_clean_main_remains_trusted_after_source_changes_or_removal(self) -> None:
        self.installed({"revision": self.revision, "dirty": False, "branch": "refs/heads/main"})
        for change in ("advance", "dirty", "feature", "remove"):
            with self.subTest(change=change):
                if change == "advance":
                    self.git("-c", "user.name=Runtime test", "-c", "user.email=runtime@example.test",
                             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                             "commit", "--allow-empty", "-m", "Advance main")
                elif change == "dirty":
                    (self.source / "scratch.txt").write_text("local work\n")
                elif change == "feature":
                    self.git("switch", "-c", "feature")
                else:
                    shutil.rmtree(self.source)
                result = self.cli("config", "init")
                self.assertEqual(result.returncode, 0, result.stderr)
        result = self.cli("skills", "install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / ".agents/skills/baton-workflow/SKILL.md").is_file())

    def test_installed_dirty_feature_detached_and_unknown_builds_refuse_writes(self) -> None:
        self.installed({})
        for provenance in ({}, [], {"revision": self.revision, "dirty": False},
                           {"revision": self.revision, "dirty": True, "branch": "refs/heads/main"},
                           {"revision": self.revision, "dirty": False, "branch": "refs/heads/feature"},
                           {"revision": self.revision, "dirty": False, "branch": ""},
                           {"revision": "invalid", "dirty": False, "branch": "refs/heads/main"}):
            with self.subTest(provenance=provenance):
                self.stamp.write_text(json.dumps(provenance))
                self.assert_refused("config", "init")
        self.stamp.write_text("invalid json")
        self.assert_refused("config", "init")
        self.stamp.unlink()
        self.assert_refused("config", "init")


if __name__ == "__main__":
    unittest.main()
